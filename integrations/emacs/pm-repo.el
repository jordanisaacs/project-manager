;;; pm-repo.el --- Magit-section canonical-repo listing -*- lexical-binding: t -*-

;;; Commentary:

;; Replaces `pm-repo-list' with a magit-section buffer.  Single
;; section, rows per repo.  RET pulls the repo at point.

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)
(require 'pm-commands)
(require 'pm-ui)  ; for `pm-repo-pull'

(defvar-local pm-repo--offline nil
  "Whether the current buffer was populated with `--offline'.")

(defvar-local pm-repo--data nil)

(defvar pm-repo-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (define-key map (kbd "g")   #'pm-repo-refresh)
    (define-key map (kbd "RET") #'pm-repo-pull-at-point)
    (define-key map (kbd "F")   #'pm-repo-pull-at-point)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `pm-repo-mode'.")

(define-derived-mode pm-repo-mode magit-section-mode "PM Repos"
  "Magit-style listing of canonical repos.

\\{pm-repo-mode-map}"
  (setq-local revert-buffer-function
              (lambda (&rest _) (pm-repo-refresh)))
  (pm-section-setup-margin))

(defun pm-repo--render ()
  (let ((inhibit-read-only t)
        (rows pm-repo--data)
        (line (line-number-at-pos)))
    (pm-table-reset-section-state)
    (erase-buffer)
    (magit-insert-section (pm-repos nil)
      ;; Root has no heading — see pm-status for the rationale.
      (insert (propertize (format "Repos (%d)" (length rows)) 'face 'bold))
      (insert "\n")
      (let* ((header '("Repo" "Branch" "Dirty" "Ahead" "Behind" "Submods"))
             (cell-rows (cons header (mapcar #'pm-repo--cells rows)))
             (widths (pm-table-widths cell-rows)))
        (insert (propertize (pm-table-row header widths)
                            'face 'magit-section-heading))
        (insert "\n")
        (dolist (row rows)
          (magit-insert-section (pm-repo row)
            (insert (pm-table-row (pm-repo--cells row) widths))
            (insert "\n")))))
    (pm-table-cover-root-section)
    (pm-table-show-root-section)
    (goto-char (point-min))
    (forward-line (1- line))))

(defun pm-repo--cells (row)
  (list (or (alist-get 'repo row) "")
        (or (alist-get 'branch row) "")
        (if (eq (alist-get 'dirty row) t) "yes" "no")
        (format "%s" (or (alist-get 'ahead row) 0))
        (format "%s" (or (alist-get 'behind row) 0))
        (or (alist-get 'submodules row) "")))

(defun pm-repo-refresh ()
  "Refetch `pm repo ls --json' and re-render."
  (interactive)
  (let ((buf (current-buffer))
        (offline pm-repo--offline))
    (pm--repo-ls
     offline
     (lambda (rows)
       (when (buffer-live-p buf)
         (with-current-buffer buf
           (setq pm-repo--data rows)
           (pm-repo--render)))))))

(defun pm-repo-pull-at-point ()
  "Pull the repo at point."
  (interactive)
  (let* ((sec (magit-current-section))
         (target (pm-section-ancestor-of-type sec '(pm-repo))))
    (unless target (user-error "No repo at point"))
    (let ((repo (alist-get 'repo (oref target value))))
      (pm-repo-pull repo))))

;;;###autoload
(defun pm-repo-list (&optional offline)
  "List canonical repos; with prefix arg, OFFLINE (skip fetch)."
  (interactive "P")
  (let ((buf (get-buffer-create "*pm-repos*")))
    (with-current-buffer buf
      (pm-repo-mode)
      (setq pm-repo--offline offline)
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "Loading…\n"))
      (pm-repo-refresh))
    (pop-to-buffer buf)
    (pm-section-set-window-margin)))

(provide 'pm-repo)

;;; pm-repo.el ends here
