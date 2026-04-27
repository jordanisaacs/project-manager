;;; pm-project.el --- project.el integration for pm -*- lexical-binding: t -*-

;;; Commentary:

;; A `project.el' backend (`'pm') that recognizes pm containers (dirs
;; with `.pm.db' under `pm-projects-dir') as projects.  The backend
;; federates `project-files' across attached worktrees so
;; `project-find-file' from the container sees the entire working set.
;; A `:around' advice on `project-name' renders worktree buffers as
;; `<container>/<wt-alias>' so the parent is visible everywhere a
;; project name is shown (modeline, consult-project-buffer, etc.).
;; Helpers and interactive commands implement parent/sibling
;; navigation between subprojects of the same pm container.

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'project)
(require 'pm-process)
(require 'pm-commands)

(defvar pm-projects-dir)
(defvar pm-include-files-from-worktrees)

(defvar pm--known-projects nil
  "Alist (CONTAINER . PLIST) of pm projects known to Emacs.
PLIST has at least :worktrees, a list of alists straight from
`pm project ls --json' rows.  Populated by `pm-refresh'.")

(defvar pm--slot-to-display nil
  "Alist (RESOLVED-SLOT-PATH . \"container/alias\") for advice lookup.
Filled at refresh time so `project-name' advice can attribute
buffers visited via the resolved (truename) worktree path.")

;;;; Path utilities

(defun pm--projects-root ()
  (file-name-as-directory (expand-file-name pm-projects-dir)))

(defun pm--under-projects-p (path)
  (string-prefix-p (pm--projects-root)
                   (file-name-as-directory (expand-file-name path))))

(defun pm--container-of (path)
  "Return the pm container directory containing PATH, or nil."
  (let* ((expanded (file-name-as-directory
                    (expand-file-name (or path default-directory))))
         (root (pm--projects-root)))
    (when (string-prefix-p root expanded)
      (let* ((rel (substring expanded (length root)))
             (name (car (split-string rel "/" t))))
        (when (and name
                   (file-exists-p
                    (expand-file-name (concat name "/.pm.db") root)))
          (file-name-as-directory (expand-file-name name root)))))))

(defun pm--worktree-alias-of (path)
  "Return the worktree alias for PATH if under a container, else nil."
  (let* ((expanded (file-name-as-directory
                    (expand-file-name (or path default-directory))))
         (root (pm--projects-root)))
    (when (string-prefix-p root expanded)
      (let* ((rel (substring expanded (length root)))
             (parts (split-string rel "/" t)))
        (when (>= (length parts) 2)
          (cadr parts))))))

(defun pm--worktree-aliases-in (container)
  "Return symlink names directly under CONTAINER (worktree aliases)."
  (let (aliases)
    (when (file-directory-p container)
      (dolist (entry (directory-files container nil "^[^.]" t))
        (let ((path (expand-file-name entry container)))
          (when (file-symlink-p path)
            (push entry aliases)))))
    (nreverse aliases)))

(defun pm--siblings-of (container current-alias)
  "Worktree aliases in CONTAINER excluding CURRENT-ALIAS."
  (let ((cached (plist-get (cdr (assoc container pm--known-projects))
                           :worktrees)))
    (cl-remove current-alias
               (if cached
                   (mapcar (lambda (row) (alist-get 'wt row)) cached)
                 (pm--worktree-aliases-in container))
               :test #'equal)))

;;;; project.el backend

;;;###autoload
(defun pm--project-finder (dir)
  "Recognize DIR as a pm container if it holds a `.pm.db'.

Only claims when DIR is the container directory itself; deeper
paths (inside a worktree symlink) are left to `project-try-vc' so
git remains the project authority for files inside a worktree."
  (let* ((expanded (file-name-as-directory (expand-file-name dir))))
    (when (and (pm--under-projects-p expanded)
               (file-exists-p (expand-file-name ".pm.db" expanded)))
      (cons 'pm expanded))))

(cl-defmethod project-root ((project (head pm)))
  (cdr project))

(cl-defmethod project-name ((project (head pm)))
  (file-name-nondirectory (directory-file-name (cdr project))))

(cl-defmethod project-files ((project (head pm)) &optional dirs)
  (ignore dirs)
  (let ((root (cdr project)))
    (if (not pm-include-files-from-worktrees)
        (cl-remove-if
         (lambda (f) (string-suffix-p "/.pm.db" f))
         (directory-files root t "^[^.]" t))
      (let (out)
        (dolist (alias (pm--worktree-aliases-in root))
          (let* ((symlink (expand-file-name alias root))
                 (target (ignore-errors (file-truename symlink))))
            (when (and target (file-directory-p target))
              (let ((sub-proj (project-current nil target)))
                (when sub-proj
                  (let ((sub-files (ignore-errors (project-files sub-proj))))
                    (dolist (f sub-files)
                      (push (concat (file-name-as-directory symlink)
                                    (file-relative-name f target))
                            out))))))))
        (nreverse out)))))

;;;; project-name advice (Pattern 3)

(defun pm--display-name-from-root (root)
  "Return `<container>/<alias>' if ROOT is a worktree, else nil.

ROOT may be the symlink path under `pm-projects-dir' or the
resolved slot path.  Container roots (parent of an alias) return
nil so the bare container name from `project-name' is preserved.
Falls back to the slot-path cache when ROOT looks like
`<projects-dir>/<x>/...' but `<x>' has no `.pm.db' (e.g. when the
worktree pool itself happens to be nested under `pm-projects-dir')."
  (let* ((expanded (file-name-as-directory (expand-file-name root)))
         (projects-root (pm--projects-root))
         (cached (cdr (assoc expanded pm--slot-to-display))))
    (or
     (and (string-prefix-p projects-root expanded)
          (let* ((rel (substring expanded (length projects-root)))
                 (parts (split-string rel "/" t)))
            (and (>= (length parts) 2)
                 (file-exists-p
                  (expand-file-name (concat (car parts) "/.pm.db")
                                    projects-root))
                 (concat (car parts) "/" (cadr parts)))))
     cached)))

(defun pm--name-advice (orig project)
  "Render worktree projects as `<container>/<alias>'."
  (or (and project
           (not (eq (car-safe project) 'pm))
           (let ((root (ignore-errors (project-root project))))
             (and root (pm--display-name-from-root root))))
      (funcall orig project)))

(unless (advice-member-p #'pm--name-advice 'project-name)
  (advice-add 'project-name :around #'pm--name-advice))

;;;; Refresh / reconcile

(defun pm--register-container (container)
  "Add CONTAINER to `project--list' as a pm project."
  (project-remember-project (cons 'pm container)))

(defun pm--forget-container (container)
  "Drop CONTAINER from `project--list' and the cache."
  (project-forget-project container)
  (setq pm--known-projects
        (assoc-delete-all container pm--known-projects)))

(defun pm--reconcile (rows)
  "Reconcile cache + `project--list' against ROWS from `pm project ls'."
  (let (new-known new-slot containers)
    (dolist (row rows)
      (let* ((name (alist-get 'project row))
             (worktrees (alist-get 'worktrees row))
             (container (file-name-as-directory
                         (expand-file-name name pm-projects-dir))))
        (push container containers)
        (push (cons container (list :worktrees worktrees)) new-known)
        (dolist (wt worktrees)
          (let* ((alias (alist-get 'wt wt))
                 (symlink (expand-file-name alias container))
                 (target (and (file-symlink-p symlink)
                              (ignore-errors (file-truename symlink)))))
            (when (and target (file-directory-p target))
              (push (cons (file-name-as-directory target)
                          (concat name "/" alias))
                    new-slot))))))
    (setq pm--known-projects (nreverse new-known))
    (setq pm--slot-to-display (nreverse new-slot))
    (dolist (c containers)
      (pm--register-container c))
    (when (and (boundp 'project--list) (listp project--list))
      (let ((live (mapcar #'identity containers))
            (root (pm--projects-root)))
        (dolist (entry (copy-sequence project--list))
          (let ((path (and (consp entry) (car entry))))
            (when (and (stringp path)
                       (string-prefix-p root path)
                       (not (member path live)))
              (project-forget-project path))))))
    (when (fboundp 'project--write-project-list)
      (project--write-project-list))
    containers))

;;;###autoload
(defun pm-refresh (&optional callback)
  "Re-sync pm projects with `project.el's known list.

Runs `pm project ls --json' asynchronously; on success reconciles
`project--list' and the local cache.  CALLBACK, if given, is
called with the list of container paths after reconciliation."
  (interactive)
  (pm--project-ls
   (lambda (rows)
     (let ((containers (pm--reconcile rows)))
       (when callback (funcall callback containers))
       (when (called-interactively-p 'any)
         (message "pm: %d project%s refreshed"
                  (length containers)
                  (if (= 1 (length containers)) "" "s")))))))

;;;; Subproject navigation (Pattern 2)

;;;###autoload
(defun pm-jump-parent ()
  "From a worktree buffer, switch to the parent pm container project."
  (interactive)
  (let ((container (pm--container-of default-directory)))
    (unless container
      (user-error "Not inside a pm-managed worktree"))
    (project-switch-project container)))

(defun pm--read-sibling (prompt)
  (let* ((container (or (pm--container-of default-directory)
                        (user-error "Not inside a pm-managed worktree")))
         (current (pm--worktree-alias-of default-directory))
         (siblings (pm--siblings-of container current)))
    (unless siblings
      (user-error "No sibling worktrees in %s"
                  (file-name-nondirectory (directory-file-name container))))
    (let ((choice (completing-read prompt siblings nil t)))
      (cons container choice))))

;;;###autoload
(defun pm-jump-sibling ()
  "Switch to a sibling worktree of the current pm container."
  (interactive)
  (pcase-let ((`(,container . ,alias) (pm--read-sibling "Sibling worktree: ")))
    (project-switch-project (expand-file-name alias container))))

(provide 'pm-project)

;;; pm-project.el ends here
