;;; pm-pool.el --- Magit-section pool slot listing -*- lexical-binding: t -*-

;;; Commentary:

;; Replaces `pm-pool-list' (`tabulated-list-mode') with a magit-section
;; buffer.  Sections per repo; rows per slot.  RET on a slot opens its
;; on-disk path via `dired'.

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)
(require 'pm-commands)
(require 'pm-ui)  ; for `pm-pool-add'

(defvar-local pm-pool--repo nil
  "Optional repo filter for the current `*pm-pool*' buffer.")

(defvar-local pm-pool--data nil
  "Last parsed `pm pool ls --json' result for this buffer.")

(defvar pm-pool-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (define-key map (kbd "g")   #'pm-pool-refresh)
    (define-key map (kbd "RET") #'pm-pool-act-at-point)
    (define-key map (kbd "+")   #'pm-pool-add)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `pm-pool-mode'.")

(define-derived-mode pm-pool-mode magit-section-mode "PM Pool"
  "Magit-style listing of pool slots.

\\{pm-pool-mode-map}"
  (setq-local revert-buffer-function
              (lambda (&rest _) (pm-pool-refresh))))

(defun pm-pool--render ()
  (let ((inhibit-read-only t)
        (groups pm-pool--data)
        (line (line-number-at-pos)))
    (pm-table-reset-section-state)
    (erase-buffer)
    (magit-insert-section (pm-pool nil)
      (magit-insert-heading
        (propertize (format "Pool slots (%d repo%s)"
                            (length groups)
                            (if (= 1 (length groups)) "" "s"))
                    'face 'bold))
      (let* ((all-cells
              (mapcan
               (lambda (group)
                 (mapcar #'pm-pool--slot-cells (alist-get 'slots group)))
               groups))
             (header '("UUID" "Claim" "Path"))
             (cell-rows (cons header all-cells))
             (widths (pm-table-widths cell-rows)))
        (insert (propertize (pm-table-row header widths)
                            'face 'magit-section-heading))
        (insert "\n")
        (dolist (group groups)
          (magit-insert-section (pm-pool-repo group)
            (magit-insert-heading
              (propertize (or (alist-get 'repo group) "")
                          'face 'pm-stacker-repo))
            (dolist (slot (alist-get 'slots group))
              (let ((cells (pm-pool--slot-cells slot)))
                (magit-insert-section (pm-pool-slot slot)
                  (insert (pm-table-row cells widths))
                  (insert "\n"))))))))
    (goto-char (point-min))
    (forward-line (1- line))))

(defun pm-pool--slot-cells (slot)
  (list (propertize (or (alist-get 'uuid slot) "") 'face 'pm-id)
        (or (alist-get 'claim slot) "")
        (or (alist-get 'path slot) "")))

(defun pm-pool-refresh ()
  "Refetch `pm pool ls --json' and re-render."
  (interactive)
  (let ((buf (current-buffer))
        (repo pm-pool--repo))
    (pm--pool-ls
     repo
     (lambda (rows)
       (when (buffer-live-p buf)
         (with-current-buffer buf
           (setq pm-pool--data rows)
           (pm-pool--render)))))))

(defun pm-pool-act-at-point ()
  "Open the slot path at point in `dired'."
  (interactive)
  (let* ((sec (magit-current-section))
         (target (pm-section-ancestor-of-type sec '(pm-pool-slot))))
    (unless target (user-error "No slot at point"))
    (let ((path (alist-get 'path (oref target value))))
      (unless (and path (file-exists-p path))
        (user-error "Slot path not on disk: %s" path))
      (dired path))))

;;;###autoload
(defun pm-pool-list (&optional repo)
  "Show pool slots; optionally filtered by REPO."
  (interactive (list (when current-prefix-arg
                       (read-string "Repo (empty = all): "))))
  (let ((buf (get-buffer-create "*pm-pool*")))
    (with-current-buffer buf
      (pm-pool-mode)
      (setq pm-pool--repo (and repo (not (string-empty-p repo)) repo))
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "Loading…\n"))
      (pm-pool-refresh))
    (pop-to-buffer buf)))

(provide 'pm-pool)

;;; pm-pool.el ends here
