;;; pm-ui.el --- Interactive entrypoints for pm -*- lexical-binding: t -*-

;;; Commentary:

;; Interactive UI built on the low-level wrappers in `pm-commands' and
;; the project.el integration in `pm-project'.  Functions here are the
;; suffix bodies for `pm-transient'.

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'project)
(require 'pm-process)
(require 'pm-commands)
(require 'pm-project)

(defvar pm-projects-dir)
(defvar pm-confirm-destructive)

;;;; Prompt helpers

(defun pm--scan-containers ()
  "Filesystem fallback: containers under `pm-projects-dir' with `.pm.db'."
  (when (file-directory-p pm-projects-dir)
    (cl-remove-if-not
     (lambda (name)
       (file-exists-p (expand-file-name (concat name "/.pm.db")
                                        pm-projects-dir)))
     (directory-files pm-projects-dir nil "^[^.]"))))

(defun pm--cached-project-names ()
  (mapcar (lambda (entry)
            (file-name-nondirectory (directory-file-name (car entry))))
          pm--known-projects))

(defun pm--read-project (prompt &optional default)
  (let ((candidates (or (pm--cached-project-names) (pm--scan-containers))))
    (unless candidates
      (user-error "No pm projects under %s" pm-projects-dir))
    (completing-read prompt candidates nil t nil nil default)))

(defun pm--read-worktree (prompt name)
  "Prompt for a worktree alias inside project NAME."
  (let* ((entry (assoc (file-name-as-directory
                        (expand-file-name name pm-projects-dir))
                       pm--known-projects))
         (worktrees (plist-get (cdr entry) :worktrees))
         (candidates
          (or (mapcar (lambda (w) (alist-get 'wt w)) worktrees)
              (pm--worktree-aliases-in
               (file-name-as-directory
                (expand-file-name name pm-projects-dir))))))
    (unless candidates
      (user-error "No worktrees in project %s" name))
    (completing-read prompt candidates nil t)))

(defun pm--read-worktrees-multi (prompt name)
  (completing-read-multiple
   prompt
   (let* ((entry (assoc (file-name-as-directory
                         (expand-file-name name pm-projects-dir))
                        pm--known-projects))
          (worktrees (plist-get (cdr entry) :worktrees)))
     (or (mapcar (lambda (w) (alist-get 'wt w)) worktrees)
         (pm--worktree-aliases-in
          (file-name-as-directory
           (expand-file-name name pm-projects-dir)))))
   nil t))

(defun pm--maybe-confirm (prompt)
  "Return non-nil if action should proceed; honors `pm-confirm-destructive'."
  (or (not pm-confirm-destructive) (yes-or-no-p prompt)))

;;;; Project lifecycle

(defun pm--build-spec (repos)
  "Prompt for an optional alias per repo in REPOS; return spec string."
  (let ((parts
         (mapcar
          (lambda (repo)
            (let ((alias (read-string
                          (format "Alias for %s (RET = none): " repo))))
              (if (string-empty-p alias) repo
                (concat alias ":" repo))))
          repos)))
    (mapconcat #'identity parts ",")))

;;;###autoload
(defun pm-project-create ()
  "Create a new pm project, attaching one or more worktrees."
  (interactive)
  (let ((name (read-string "New project name: ")))
    (when (string-empty-p name) (user-error "Aborted"))
    (pm--repo-ls
     nil
     (lambda (repos)
       (let* ((repo-names (mapcar (lambda (r) (alist-get 'repo r)) repos))
              (chosen (completing-read-multiple
                       "Worktrees (pick repos, comma-sep): "
                       repo-names nil t)))
         (when (null chosen)
           (user-error "Aborted: at least one worktree required"))
         (let ((spec (pm--build-spec chosen)))
           (pm--project-create
            name spec
            (lambda (rows)
              (let ((container (file-name-as-directory
                                (expand-file-name name pm-projects-dir))))
                (pm-refresh
                 (lambda (_)
                   (let ((aliases (mapcar (lambda (r) (alist-get 'wt r)) rows)))
                     (message "pm: created %s [%s]"
                              name (mapconcat #'identity aliases ", "))
                     (when (and aliases
                                (y-or-n-p
                                 (format "Switch into %s/%s? "
                                         name (car aliases))))
                       (project-switch-project
                        (expand-file-name (car aliases) container)))))))))))))))

;;;###autoload
(defun pm-project-delete (name)
  "Delete pm project NAME (and all its worktrees)."
  (interactive (list (pm--read-project "Delete project: ")))
  (when (pm--maybe-confirm
         (format "Really delete pm project %s and all its worktrees? " name))
    (pm--project-delete
     name nil
     (lambda (_)
       (pm--forget-container
        (file-name-as-directory (expand-file-name name pm-projects-dir)))
       (message "pm: deleted %s" name)))))

;;;###autoload
(defun pm-project-switch (name)
  "Switch into pm project NAME."
  (interactive (list (pm--read-project "Switch to project: ")))
  (project-switch-project
   (file-name-as-directory (expand-file-name name pm-projects-dir))))

;;;; Worktree commands

;;;###autoload
(defun pm-wt-create (name)
  "Add a worktree to project NAME."
  (interactive (list (pm--read-project "Add worktree to: ")))
  (pm--repo-ls
   nil
   (lambda (repos)
     (let* ((repo-names (mapcar (lambda (r) (alist-get 'repo r)) repos))
            (chosen (completing-read-multiple
                     "Worktrees (repos, comma-sep): " repo-names nil t)))
       (when (null chosen) (user-error "Aborted"))
       (let ((spec (pm--build-spec chosen)))
         (pm--wt-create
          name spec
          (lambda (_)
            (pm-refresh
             (lambda (_) (message "pm: added wt(s) to %s" name))))))))))

(defun pm--wt-action (action verb name)
  "Common skeleton for attach/detach/delete on worktrees in NAME.
ACTION is the wrapper symbol; VERB is the user-visible name."
  (let* ((all (y-or-n-p (format "%s ALL worktrees in %s? " verb name)))
         (wts (unless all
                (mapconcat #'identity
                           (pm--read-worktrees-multi
                            (format "%s which worktrees: " verb) name)
                           ","))))
    (when (and (member verb '("Detach" "Delete"))
               (not (pm--maybe-confirm
                     (format "%s %s in %s? "
                             verb (if all "all worktrees" wts) name))))
      (user-error "Aborted"))
    (cond
     ((eq action 'attach) (pm--wt-attach name wts all nil
                                         (lambda (_)
                                           (pm-refresh
                                            (lambda (_) (message "pm: attached"))))))
     ((eq action 'detach) (pm--wt-detach name wts all nil
                                         (lambda (_)
                                           (pm-refresh
                                            (lambda (_) (message "pm: detached"))))))
     ((eq action 'delete) (pm--wt-delete name wts all
                                         (lambda (_)
                                           (pm-refresh
                                            (lambda (_) (message "pm: deleted"))))))
     ((eq action 'create) (error "Use pm-wt-create directly")))))

;;;###autoload
(defun pm-wt-attach (name)
  (interactive (list (pm--read-project "Attach worktrees in: ")))
  (pm--wt-action 'attach "Attach" name))

;;;###autoload
(defun pm-wt-detach (name)
  (interactive (list (pm--read-project "Detach worktrees in: ")))
  (pm--wt-action 'detach "Detach" name))

;;;###autoload
(defun pm-wt-delete (name)
  (interactive (list (pm--read-project "Delete worktrees in: ")))
  (pm--wt-action 'delete "Delete" name))

;;;; Pool / repo verbs (the listing buffers live in `pm-pool', `pm-repo')

;;;###autoload
(defun pm-pool-add (repo)
  "Mint a new pool slot for REPO."
  (interactive (list (read-string "Repo to mint slot for: ")))
  (when (string-empty-p repo) (user-error "Aborted"))
  (pm--pool-add
   repo
   (lambda (rows)
     (message "pm: minted slot(s) %s"
              (mapconcat (lambda (r) (or (alist-get 'uuid r) "?"))
                         rows ", ")))))

;;;###autoload
(defun pm-repo-pull (&optional repo)
  "Fetch all canonical repos; with prefix arg, prompt for one REPO."
  (interactive (list (when current-prefix-arg
                       (read-string "Repo to pull: "))))
  (message "pm: pulling%s..." (if repo (format " %s" repo) " all repos"))
  (pm--repo-pull
   (and repo (not (string-empty-p repo)) repo)
   (lambda (_) (message "pm: pull complete"))))

(provide 'pm-ui)

;;; pm-ui.el ends here
