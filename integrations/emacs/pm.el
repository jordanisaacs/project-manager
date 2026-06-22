;;; pm.el --- Emacs integration for pm CLI -*- lexical-binding: t -*-

;; Author: Jordan Isaacs
;; Version: 0.2.0
;; Package-Requires: ((emacs "28.1") (transient "0.4") (magit-section "4.0"))
;; Keywords: tools, vc

;;; Commentary:

;; Drives the `pm' CLI (project_manager: a pool of git worktrees bound
;; to named projects via symlinks) from inside Emacs.  Recognizes pm
;; containers as `project.el' projects via a custom backend keyed on
;; the `.pm.db' marker, auto-registers them so they appear in
;; `C-x p p', and exposes a transient menu (`pm-transient') for
;; project / worktree / pool / repo verbs.  Each worktree under a
;; container is treated as a "subproject": container `project-files'
;; federates across worktrees, the transient has a Subproject group
;; for parent / sibling navigation, and `project-name' is advised to
;; render worktree buffers as `<container>/<wt-alias>'.

;;; Code:

(require 'cl-lib)

;;;###autoload
(defgroup pm nil
  "Emacs integration for the pm (project_manager) CLI."
  :group 'tools
  :prefix "pm-")

;; The defcustoms are autoload-cookied so twist's pm-autoloads.el
;; surfaces them eagerly — submodules (pm-process, pm-project, ...)
;; reference these variables before pm.el itself is loaded.

;;;###autoload
(defcustom pm-executable "pm"
  "Path or name of the `pm' executable.
Resolved via `executable-find' on first use."
  :type 'string
  :group 'pm)

;;;###autoload
(defcustom pm-projects-dir (expand-file-name "~/.projects/")
  "Root directory under which pm projects (containers) live.
Each container is `<pm-projects-dir>/<name>/' and contains a
`.pm.db' file plus one symlink per attached worktree."
  :type 'directory
  :group 'pm)

;;;###autoload
(defcustom pm-confirm-destructive t
  "When non-nil, confirm destructive pm operations.
Applies to `project delete', `wt detach --all', `wt remove', etc."
  :type 'boolean
  :group 'pm)

;;;###autoload
(defcustom pm-include-files-from-worktrees t
  "When non-nil, container `project-files' federates across worktrees.
Each entry is prefixed with the worktree alias (e.g. `emacs/init.el').
Set to nil to list only top-level container entries (cheaper for
containers with many large worktrees)."
  :type 'boolean
  :group 'pm)

;;;###autoload
(defcustom pm-manager-project nil
  "Project name used as `default-directory' for global pm buffers.
Applies to `pm-list', `pm-pool', `pm-repo-list', and `pm-agent-list'
in its all-projects view — none of which are bound to a single
project but still benefit from a sensible cwd (e.g. so a transient
opened from the buffer scopes to your usual workspace).
When nil, those buffers fall back to the user's home directory."
  :type '(choice (const :tag "Home directory" nil) string)
  :group 'pm)

;;;###autoload
(defun pm-global-default-directory ()
  "Resolve `default-directory' for non-project-scoped pm buffers.
Returns `pm-manager-project's container under `pm-projects-dir'
when set, else the user's home directory."
  (file-name-as-directory
   (expand-file-name
    (if pm-manager-project
        (expand-file-name pm-manager-project pm-projects-dir)
      "~"))))

(require 'pm-process)
(require 'pm-project)
(require 'pm-commands)

;; pm-ui, pm-transient, and the magit-section buffer modules are loaded
;; on demand via autoloads.  Heavy modules (status / agent / list /
;; pool / repo) only load when the user actually opens that buffer.
(autoload 'pm-dispatch "pm-transient" nil t)
(autoload 'pm-project-dispatch "pm-transient" nil t)
(autoload 'pm-agent-launch-dispatch "pm-transient" nil t)
(autoload 'pm-project-create "pm-ui" nil t)
(autoload 'pm-project-delete "pm-ui" nil t)
(autoload 'pm-project-switch "pm-ui" nil t)
(autoload 'pm-wt-add "pm-ui" nil t)
(autoload 'pm-wt-attach "pm-ui" nil t)
(autoload 'pm-wt-detach "pm-ui" nil t)
(autoload 'pm-wt-remove "pm-ui" nil t)
(autoload 'pm-pool-add "pm-ui" nil t)
(autoload 'pm-repo-pull "pm-ui" nil t)
(autoload 'pm-project-status "pm-status" nil t)
(autoload 'pm-stacker-list "pm-stacker" nil t)
(autoload 'pm-stacker-dispatch "pm-transient" nil t)
(autoload 'pm-project-list "pm-list" nil t)
(autoload 'pm-pool-list "pm-pool" nil t)
(autoload 'pm-repo-list "pm-repo" nil t)
(autoload 'pm-agent-list "pm-agent" nil t)
(autoload 'pm-agent-list-current-project "pm-agent" nil t)
(autoload 'pm-agent-launch "pm-agent" nil t)
(autoload 'pm-agent-dispatch-term "pm-agent" nil t)
(autoload 'pm-agent-dispatch-vterm "pm-agent" nil t)
(autoload 'pm-agent-dispatch-ghostel "pm-agent" nil t)
(autoload 'pm-sidebar "pm-sidebar" nil t)

(provide 'pm)

;;; pm.el ends here
