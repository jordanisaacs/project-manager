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

;; The magit-section buffer entry points are autoloaded from pm.el;
;; declare so the byte-compiler can resolve their symbols here.
(declare-function pm-project-status            "pm-status" (name))
(declare-function pm-agent-list-current-project "pm-agent" ())
(declare-function pm-agent-launch              "pm-agent" (agent &optional project))

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

(transient-define-suffix pm-project-dispatch--wt-add ()
  "Add a worktree to the bound project."
  :description "new"
  (interactive)
  (pm-wt-add (pm--ds-container-name)))

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

(transient-define-suffix pm-project-dispatch--wt-remove ()
  "Remove worktrees from the bound project, honoring infix args."
  :description
  (lambda ()
    (let ((args (pm--dispatch-args)))
      (pm--decorate-desc "remove" (and (member "--all" args) "all"))))
  (interactive)
  (let* ((args (pm--dispatch-args))
         (all (and (member "--all" args) t))
         (name (pm--ds-container-name))
         (wts (unless all
                (mapconcat #'identity
                           (pm--read-worktrees-multi
                            "Remove which: " name)
                           ","))))
    (when (pm--maybe-confirm
           (format "Permanently remove %s in %s? "
                   (if all "all worktrees" wts) name))
      (pm--wt-remove name wts all
                     (lambda (_)
                       (pm-refresh
                        (lambda (_) (message "pm: removed"))))))))

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

(transient-define-suffix pm-project-dispatch--agent ()
  "Enter `pm-agent-launch-dispatch' on the bound project.
The parent's `:scope' is forwarded so the sub-transient acts on the
same project the user picked here, not on `default-directory' (which
may differ when `pm-project-dispatch' was entered with a prefix arg).
Following the convention used elsewhere in this file (see the global
dispatch `.' suffix), the parent transient exits via the default
suffix behavior and our body then sets up the child."
  :description "run →"
  (interactive)
  (transient-setup 'pm--agent-launch-dispatch-menu nil nil
                   :scope (pm--ds-path)))

;;;; Agent-launch sub-dispatch
;;
;; A focused mini-transient for `pm agent {claude,codex,cursor}'.
;; Reachable from three entry points, all of which set `:scope' to the
;; pm project's path:
;;
;;   - `pm-agent-launch-dispatch' (autoloaded standalone — used by
;;     `project-switch-commands' to bind `?a').
;;   - `pm-project-dispatch' → `r' (forwards parent scope).
;;   - `pm-dispatch'         → `r' (prompts when not in a pm tree).
;;
;; Suffixes read `(transient-scope)' just like the project-dispatch
;; suffixes do — `default-directory' would be wrong because it can have
;; reverted by the time the user presses a key.

(transient-define-suffix pm-agent-launch-dispatch--claude ()
  "Launch Claude Code in the bound project."
  :description "claude"
  (interactive)
  (pm-agent-launch "claude" (pm--ds-container-name)))

(transient-define-suffix pm-agent-launch-dispatch--codex ()
  "Launch Codex in the bound project."
  :description "codex"
  (interactive)
  (pm-agent-launch "codex" (pm--ds-container-name)))

(transient-define-suffix pm-agent-launch-dispatch--cursor ()
  "Launch Cursor in the bound project."
  :description "cursor"
  (interactive)
  (pm-agent-launch "cursor" (pm--ds-container-name)))

(transient-define-prefix pm--agent-launch-dispatch-menu ()
  "pm: launch a fresh agent in the bound project."
  [:description pm--dispatch-header
   ["Launch"
    ("c" pm-agent-launch-dispatch--claude)
    ("o" pm-agent-launch-dispatch--codex)
    ("u" pm-agent-launch-dispatch--cursor)]
   ["System"
    ("<" "back" pm-project-dispatch)]])

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
    ("n" pm-project-dispatch--wt-add)
    ("a" pm-project-dispatch--wt-attach)
    ("D" pm-project-dispatch--wt-detach)
    ("X" pm-project-dispatch--wt-remove)]
   ["Navigate"
    :if pm--ds-inside-worktree-p
    ("p" pm-project-dispatch--jump-parent)
    ("j" pm-project-dispatch--jump-sibling :if pm--ds-has-siblings-p)]]
  [["Agent"
    ("r" pm-project-dispatch--agent)
    ("l" "sessions" pm-agent-list-current-project)]
   ["System"
    ("g" "refresh"   pm-refresh)
    ("<" "global pm" pm-dispatch)]
   ["Danger"
    ("K" pm-project-dispatch--delete)]])

;; Shared resolver: the bound project travels via `:scope' rather than
;; `default-directory' — `transient-setup' returns immediately, and any
;; `let' around the entry call would unwind before the user pressed a
;; suffix key.
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
(defun pm--resolve-dispatch-path (arg)
  "Resolve the pm project path for a dispatch entry.
With prefix ARG, always prompt; otherwise honor the override / cwd /
prompt fallback chain documented above."
  (let ((override (bound-and-true-p project-current-directory-override)))
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

;;;###autoload
(defun pm-project-dispatch (&optional arg)
  "Open `pm-project-dispatch' on the current pm project.
With prefix ARG, prompt for a project even when one is contextually
available."
  (interactive "P")
  (transient-setup 'pm--project-dispatch-menu nil nil
                   :scope (pm--resolve-dispatch-path arg)))

;;;###autoload
(defun pm-agent-launch-dispatch (&optional arg)
  "Open the agent-launch sub-dispatch on the current pm project.
Designed to be bound under `project-switch-commands' (e.g. `?a') so
that pressing a single key after `C-x p p <project>' offers a fresh
launch of claude / codex / cursor scoped to the just-picked project.

With prefix ARG, prompt for a project even when one is contextually
available."
  (interactive "P")
  (transient-setup 'pm--agent-launch-dispatch-menu nil nil
                   :scope (pm--resolve-dispatch-path arg)))

;;;; Global dispatch
;;
;; Grouped by noun (Project / Pool / Repos / Agent), not by verb, so
;; every action against a given resource sits in one column.  The
;; Agent group's `r' enters `pm-agent-launch-dispatch', which prompts
;; for a project (no pm tree at the global level).

;;;###autoload (autoload 'pm-dispatch "pm-transient" nil t)
(transient-define-prefix pm-dispatch ()
  "pm: global commands."
  ["pm"
   ["Project"
    ("c" "create"  pm-project-create :if pm--no-create-inflight)
    ("p" "switch"  pm-project-switch)
    ("l" "list"    pm-project-list)]
   ["Pool"
    ("+" "add"     pm-pool-add)
    ("o" "list"    pm-pool-list)]
   ["Repos"
    ("F" "pull"    pm-repo-pull)
    ("R" "list"    pm-repo-list)]
   ["Agent"
    ("r" "run →"    pm-agent-launch-dispatch)
    ("a" "sessions" pm-agent-list)]]
  [["System"
    ("g" "refresh" pm-refresh)]
   ["Project"
    :if pm--in-pm-tree-p
    ("." "current project →" pm-project-dispatch)]])

(provide 'pm-transient)

;;; pm-transient.el ends here
