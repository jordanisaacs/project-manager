;;; pm-list.el --- Magit-section project × worktree list -*- lexical-binding: t -*-

;;; Commentary:

;; Replaces the old `pm-list-mode' (`tabulated-list-mode') with a
;; magit-section buffer.  Top-level sections per project; child rows
;; per worktree.  Movement / fold keys come from
;; `magit-section-mode-map'.
;;
;; Row actions:
;;   RET — switch to the project (or worktree, when on a leaf row)
;;   s   — open `pm-project-status' for the project at point
;;   D   — `pm-project-delete' the project at point

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'project)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)
(require 'pm-commands)
(require 'pm-ui)  ; for `pm-project-delete'

(defvar pm-projects-dir)

;; Forward declaration — `pm-project-status' is the magit-section
;; entry point in `pm-status.el'.
(declare-function pm-project-status "pm-status" (name))

(defvar-local pm-list--data nil
  "Last parsed `pm project ls --json' result for this buffer.")

(defvar pm-list-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (define-key map (kbd "g")   #'pm-list-refresh)
    (define-key map (kbd "RET") #'pm-list-act-at-point)
    (define-key map (kbd "s")   #'pm-list-status-at-point)
    (define-key map (kbd "D")   #'pm-list-delete-at-point)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `pm-list-mode'.")

(define-derived-mode pm-list-mode magit-section-mode "PM List"
  "Magit-style listing of pm projects and worktrees.

\\{pm-list-mode-map}"
  (setq-local revert-buffer-function
              (lambda (&rest _) (pm-list-refresh))))

(defun pm-list--render ()
  (let ((inhibit-read-only t)
        (rows pm-list--data)
        (line (line-number-at-pos)))
    (pm-table-reset-section-state)
    (erase-buffer)
    (magit-insert-section (pm-list nil)
      (magit-insert-heading
        (propertize (format "pm projects (%d)" (length rows)) 'face 'bold))
      (let* ((all-cells (mapcan #'pm-list--row-cells rows))
             (header '("Worktree" "Repo" "Branch" "Status"))
             (cell-rows (cons header all-cells))
             (widths (pm-table-widths cell-rows)))
        (insert (propertize (pm-table-row header widths)
                            'face 'magit-section-heading))
        (insert "\n")
        (dolist (row rows)
          (let* ((project (alist-get 'project row))
                 (wts (alist-get 'worktrees row)))
            (magit-insert-section (pm-project row)
              (magit-insert-heading
                (propertize project 'face 'pm-stacker-repo))
              (dolist (wt wts)
                (let ((cells (list (or (alist-get 'wt wt) "")
                                   (or (alist-get 'repo wt) "")
                                   (or (alist-get 'branch wt) "")
                                   (or (alist-get 'status wt) ""))))
                  (magit-insert-section (pm-worktree wt)
                    (insert (pm-table-row cells widths))
                    (insert "\n")))))))))
    (pm-table-cover-root-section)
    (goto-char (point-min))
    (forward-line (1- line))))

(defun pm-list--row-cells (project-row)
  "Return all worktree-row cell-lists for a single PROJECT-ROW (alist)."
  (mapcar (lambda (wt)
            (list (or (alist-get 'wt wt) "")
                  (or (alist-get 'repo wt) "")
                  (or (alist-get 'branch wt) "")
                  (or (alist-get 'status wt) "")))
          (alist-get 'worktrees project-row)))

(defun pm-list-refresh ()
  "Refetch `pm project ls --json' and re-render."
  (interactive)
  (let ((buf (current-buffer)))
    (pm--project-ls
     (lambda (rows)
       (when (buffer-live-p buf)
         (with-current-buffer buf
           (setq pm-list--data rows)
           (pm-list--render)))))))

(defun pm-list--project-at-point ()
  "Return the project name at point, walking ancestors as needed."
  (let* ((sec (magit-current-section))
         (ancestor (pm-section-ancestor-of-type sec '(pm-project pm-worktree))))
    (when ancestor
      (let ((value (oref ancestor value)))
        (or (alist-get 'project value)  ; `pm-project' rows
            ;; `pm-worktree' leaf — climb one more time
            (let ((parent
                   (pm-section-ancestor-of-type
                    (oref ancestor parent) '(pm-project))))
              (and parent (alist-get 'project (oref parent value)))))))))

(defun pm-list-act-at-point ()
  "Switch to the project (or worktree) at point."
  (interactive)
  (let* ((sec (magit-current-section))
         (target (pm-section-ancestor-of-type
                  sec '(pm-worktree pm-project))))
    (unless target (user-error "Nothing actionable at point"))
    (pcase (oref target type)
      ('pm-worktree
       (let* ((value (oref target value))
              (project (pm-list--project-at-point))
              (wt (alist-get 'wt value))
              (path (and project wt
                         (expand-file-name
                          wt (expand-file-name project pm-projects-dir)))))
         (unless (and path (file-exists-p path))
           (user-error "Worktree path not on disk: %s" path))
         (project-switch-project path)))
      ('pm-project
       (let* ((value (oref target value))
              (project (alist-get 'project value)))
         (project-switch-project
          (file-name-as-directory
           (expand-file-name project pm-projects-dir))))))))

(defun pm-list-status-at-point ()
  "Open `pm-project-status' for the project at point."
  (interactive)
  (let ((name (pm-list--project-at-point)))
    (unless name (user-error "No project at point"))
    (pm-project-status name)))

(defun pm-list-delete-at-point ()
  "Delete the project at point (with the standard confirm path)."
  (interactive)
  (let ((name (pm-list--project-at-point)))
    (unless name (user-error "No project at point"))
    (pm-project-delete name)))

;;;###autoload
(defun pm-project-list ()
  "List pm projects and worktrees in a magit-section buffer."
  (interactive)
  (let ((buf (get-buffer-create "*pm-list*")))
    (with-current-buffer buf
      (pm-list-mode)
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "Loading…\n"))
      (pm-list-refresh))
    (pop-to-buffer buf)))

(provide 'pm-list)

;;; pm-list.el ends here
