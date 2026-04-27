;;; pm-transient.el --- Transient menus for pm -*- lexical-binding: t -*-

;;; Commentary:

;; Two magit-style transients:
;;
;; - `pm-dispatch' (global, `C-x p P') — verbs that don't need a
;;   current project: create / list / switch / refresh, plus pool and
;;   repo verbs.  A `[Project]' group with a single `.' suffix appears
;;   only when the buffer is inside a pm tree, and enters
;;   `pm-project-dispatch'.
;;
;; - `pm-project-dispatch' (project, `C-x p .') — verbs scoped to a
;;   single pm project, derived from `default-directory' (prompts if
;;   invoked outside any pm tree).  Header shows
;;   `pm: <container>[/<alias>]'.  Persistent infix args
;;   (`-A --all', `-n --no-branch', `-d --dry-run') drive the wt
;;   action suffixes; descriptions live-update to reflect toggles.

;;; Code:

(require 'transient)
(require 'pm-process)
(require 'pm-project)
(require 'pm-ui)
(require 'pm-commands)

(defvar pm-projects-dir)
(defvar pm-confirm-destructive)

;;;; Buffer-level predicates (used by `pm-dispatch')

(defun pm--in-pm-tree-p ()
  "Non-nil when `default-directory' is inside any pm container."
  (and (pm--container-of default-directory) t))

(defun pm--no-create-inflight ()
  (not (pm--inflight-p 'create)))

;;;; Scope-level helpers (used by `pm-project-dispatch')
;;
;; The bound project travels through the transient via `:scope', not
;; via `default-directory': the entry function's let-binding unwinds
;; the moment `transient-setup' returns, well before any suffix key is
;; pressed.  Header / predicates / suffix bodies must therefore read
;; `(transient-scope)' — not the buffer's `default-directory' — when
;; computing what project they're acting on.

(defun pm--ds-path ()
  "Return the path bound to the current project dispatch, or nil."
  (transient-scope))

(defun pm--ds-container ()
  (when-let ((p (pm--ds-path))) (pm--container-of p)))

(defun pm--ds-container-name ()
  (when-let ((c (pm--ds-container)))
    (file-name-nondirectory (directory-file-name c))))

(defun pm--ds-alias ()
  (when-let ((p (pm--ds-path))) (pm--worktree-alias-of p)))

(defun pm--ds-inside-worktree-p ()
  (and (pm--ds-container) (pm--ds-alias) t))

(defun pm--ds-has-siblings-p ()
  (when-let* ((c (pm--ds-container))
              (a (pm--ds-alias)))
    (consp (pm--siblings-of c a))))

(defun pm--container-name (dir)
  "Return the bare container name for DIR, or nil."
  (when-let ((c (pm--container-of dir)))
    (file-name-nondirectory (directory-file-name c))))

(defun pm--dispatch-header ()
  (format "pm: %s%s"
          (or (pm--ds-container-name) "?")
          (if-let ((a (pm--ds-alias))) (format "/%s" a) "")))

;;;; Project-dispatch suffixes (read infix args)

(defun pm--dispatch-args ()
  (transient-args 'pm--project-dispatch-menu))

(defun pm--decorate-desc (base &rest tags)
  "Return BASE description plus parenthetical TAGS that are non-nil."
  (let ((extra (delq nil tags)))
    (if extra (format "%s (%s)" base (mapconcat #'identity extra " "))
      base)))

(transient-define-suffix pm-project-dispatch--status ()
  "Show status of the bound project."
  :description "status"
  (interactive)
  (pm-project-status (pm--ds-container-name)))

(transient-define-suffix pm-project-dispatch--wt-create ()
  "Add a worktree to the bound project."
  :description "new"
  (interactive)
  (pm-wt-create (pm--ds-container-name)))

(transient-define-suffix pm-project-dispatch--wt-attach ()
  "Attach worktrees in the bound project, honoring infix args."
  :description
  (lambda ()
    (let ((args (pm--dispatch-args)))
      (pm--decorate-desc "attach"
                         (and (member "--all" args) "all")
                         (and (member "--no-branch" args) "no-branch"))))
  (interactive)
  (let* ((args (pm--dispatch-args))
         (all (and (member "--all" args) t))
         (no-branch (and (member "--no-branch" args) t))
         (name (pm--ds-container-name))
         (wts (unless all
                (mapconcat #'identity
                           (pm--read-worktrees-multi
                            "Attach which: " name)
                           ","))))
    (pm--wt-attach name wts all no-branch
                   (lambda (_)
                     (pm-refresh
                      (lambda (_) (message "pm: attached")))))))

(transient-define-suffix pm-project-dispatch--wt-detach ()
  "Detach worktrees in the bound project, honoring infix args."
  :description
  (lambda ()
    (let ((args (pm--dispatch-args)))
      (pm--decorate-desc "detach"
                         (and (member "--all" args) "all")
                         (and (member "--dry-run" args) "dry"))))
  (interactive)
  (let* ((args (pm--dispatch-args))
         (all (and (member "--all" args) t))
         (dry-run (and (member "--dry-run" args) t))
         (name (pm--ds-container-name))
         (wts (unless all
                (mapconcat #'identity
                           (pm--read-worktrees-multi
                            "Detach which: " name)
                           ","))))
    (when (or dry-run
              (pm--maybe-confirm
               (format "Detach %s in %s? "
                       (if all "all worktrees" wts) name)))
      (pm--wt-detach name wts all dry-run
                     (lambda (_)
                       (pm-refresh
                        (lambda (_)
                          (message "pm: %s"
                                   (if dry-run "detach (dry-run)" "detached")))))))))

(transient-define-suffix pm-project-dispatch--wt-delete ()
  "Delete worktrees from the bound project, honoring infix args."
  :description
  (lambda ()
    (let ((args (pm--dispatch-args)))
      (pm--decorate-desc "delete" (and (member "--all" args) "all"))))
  (interactive)
  (let* ((args (pm--dispatch-args))
         (all (and (member "--all" args) t))
         (name (pm--ds-container-name))
         (wts (unless all
                (mapconcat #'identity
                           (pm--read-worktrees-multi
                            "Delete which: " name)
                           ","))))
    (when (pm--maybe-confirm
           (format "Permanently delete %s in %s? "
                   (if all "all worktrees" wts) name))
      (pm--wt-delete name wts all
                     (lambda (_)
                       (pm-refresh
                        (lambda (_) (message "pm: deleted"))))))))

(transient-define-suffix pm-project-dispatch--delete ()
  "Delete the bound project."
  :description "delete project"
  (interactive)
  (pm-project-delete (pm--ds-container-name)))

(transient-define-suffix pm-project-dispatch--jump-parent ()
  "Switch to the bound project's container."
  :description "parent"
  (interactive)
  (let ((default-directory (or (pm--ds-path) default-directory)))
    (call-interactively #'pm-jump-parent)))

(transient-define-suffix pm-project-dispatch--jump-sibling ()
  "Switch to a sibling worktree of the bound project."
  :description "sibling"
  (interactive)
  (let ((default-directory (or (pm--ds-path) default-directory)))
    (call-interactively #'pm-jump-sibling)))

;;;; Project dispatch (the prefix)

(transient-define-prefix pm--project-dispatch-menu ()
  "pm: act on the bound pm project."
  [:description pm--dispatch-header
   ["Arguments"
    ("-A" "All worktrees"           "--all")
    ("-n" "Skip branch restore"     "--no-branch")
    ("-d" "Dry run"                 "--dry-run")]]
  [["Inspect"
    ("s" pm-project-dispatch--status)]
   ["Worktrees"
    ("n" pm-project-dispatch--wt-create)
    ("a" pm-project-dispatch--wt-attach)
    ("D" pm-project-dispatch--wt-detach)
    ("X" pm-project-dispatch--wt-delete)]
   ["Navigate"
    :if pm--ds-inside-worktree-p
    ("p" pm-project-dispatch--jump-parent)
    ("j" pm-project-dispatch--jump-sibling :if pm--ds-has-siblings-p)]]
  [["Danger"
    ("K" pm-project-dispatch--delete)]
   ["System"
    ("g" "refresh"   pm-refresh)
    ("<" "global pm" pm-dispatch)]])

;; The bound project travels via `:scope' rather than `default-directory'
;; — `transient-setup' returns immediately, and any `let' around the
;; entry call would unwind before the user pressed a suffix key.
;;
;; Resolution order:
;; 1. With prefix ARG, always prompt.
;; 2. Else, honor `project-current-directory-override' if set — that's
;;    how `project-switch-project' (Emacs 28+) communicates the
;;    just-selected project to its post-switch command.  It does NOT
;;    rebind `default-directory'.
;; 3. Else, if the buffer's `default-directory' is inside a pm tree,
;;    use that tree.
;; 4. Else, prompt.
;;;###autoload
(defun pm-project-dispatch (&optional arg)
  "Open `pm-project-dispatch' on the current pm project.
With prefix ARG, prompt for a project even when one is contextually
available."
  (interactive "P")
  (let* ((override (bound-and-true-p project-current-directory-override))
         (path
          (cond
           (arg
            (file-name-as-directory
             (expand-file-name (pm--read-project "Project: ")
                               pm-projects-dir)))
           ((and override (pm--container-of override))
            (file-name-as-directory (expand-file-name override)))
           ((pm--container-of default-directory)
            default-directory)
           (t
            (file-name-as-directory
             (expand-file-name (pm--read-project "Project: ")
                               pm-projects-dir))))))
    (transient-setup 'pm--project-dispatch-menu nil nil :scope path)))

;;;; Global dispatch

;;;###autoload (autoload 'pm-dispatch "pm-transient" nil t)
(transient-define-prefix pm-dispatch ()
  "pm: global commands."
  ["pm"
   ["Create"
    ("c" "project"   pm-project-create :if pm--no-create-inflight)
    ("+" "pool slot" pm-pool-add)]
   ["Inspect"
    ("l" "projects"  pm-project-list)
    ("o" "pool"      pm-pool-list)
    ("R" "repos"     pm-repo-list)]
   ["Switch"
    ("p" "project"   pm-project-switch)]
   ["System"
    ("g" "refresh"   pm-refresh)
    ("F" "pull repos" pm-repo-pull)]
   ["Project"
    :if pm--in-pm-tree-p
    ("." "current project →" pm-project-dispatch)]])

(provide 'pm-transient)

;;; pm-transient.el ends here
