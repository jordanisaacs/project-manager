;;; pm-commands.el --- Low-level wrappers for pm CLI verbs -*- lexical-binding: t -*-

;;; Commentary:

;; Thin async wrappers around `pm <verb> --json'.  Each function
;; takes its arguments and a callback that receives the parsed JSON.
;; The callback is invoked only on successful (exit 0) completion;
;; non-zero results surface via `pm--surface-error' from pm-process.

;;; Code:

(require 'pm-process)

(defun pm--strs (&rest parts)
  "Build an argv list, dropping nil and flattening lists."
  (let (out)
    (dolist (p parts)
      (cond
       ((null p) nil)
       ((listp p) (dolist (x p) (when x (push x out))))
       (t (push p out))))
    (nreverse out)))

;;;; project

(defun pm--project-ls (cb)
  (pm--run-async '("project" "ls" "--json") cb))

(defun pm--project-status (name cb)
  (pm--run-async (pm--strs "project" "status" name "--json") cb))

(defun pm--project-status-section (sections name cb)
  "Run `pm project status NAME -s SECTIONS --json' asynchronously.

SECTIONS is a comma-separated string drawn from
`worktrees,prs,stacker,sessions' — passed through to the CLI's
`-s' flag.  The callback receives a JSON object whose keys are
exactly the requested sections."
  (pm--run-async
   (pm--strs "project" "status" name "-s" sections "--json")
   cb))

(defun pm--project-create (name spec cb)
  (pm--run-async
   (pm--strs "project" "create" name
             (and spec (list "--wt" spec))
             "--json")
   cb :tag 'create))

(defun pm--project-delete (name cb)
  (pm--run-async
   (pm--strs "project" "delete" name "--json")
   cb :tag 'delete))

;;;; project wt

(defun pm--wt-add (name spec cb)
  (pm--run-async
   (pm--strs "project" "wt" "add" spec "--project" name "--json")
   cb :tag 'wt-add))

(defun pm--wt-attach (name wts all no-branch cb)
  (pm--run-async
   (pm--strs "project" "wt" "attach" name
             (cond (all '("--all"))
                   (wts (list "--wt" wts)))
             (and no-branch '("--no-branch"))
             "--json")
   cb :tag 'wt-attach))

(defun pm--wt-detach (name wts all dry-run cb)
  (pm--run-async
   (pm--strs "project" "wt" "detach" name
             (cond (all '("--all"))
                   (wts (list "--wt" wts)))
             (and dry-run '("--dry-run"))
             "--json")
   cb :tag 'wt-detach))

(defun pm--wt-remove (name wts all cb)
  (pm--run-async
   (pm--strs "project" "wt" "remove" name
             (cond (all '("--all"))
                   (wts (list "--wt" wts)))
             "--json")
   cb :tag 'wt-remove))

;;;; pool

(defun pm--pool-ls (repo cb)
  (pm--run-async (pm--strs "pool" "ls" repo "--json") cb))

(defun pm--pool-add (repo cb)
  (pm--run-async (pm--strs "pool" "add" repo "--json") cb :tag 'pool-add))

;;;; repo

(defun pm--repo-ls (offline cb)
  (pm--run-async
   (pm--strs "repo" "ls" (and offline '("--offline")) "--json")
   cb))

(defun pm--repo-pull (repo cb)
  (pm--run-async
   (pm--strs "repo" "pull" (and repo (list "--repo" repo)) "--json")
   cb :tag 'repo-pull))

;;;; agent

(defun pm--agent-ls (project all limit cb)
  "Run `pm agent ls --json' (optionally scoped to PROJECT or ALL projects).

LIMIT is the integer per-project session cap (passed via `-n');
nil omits the flag and lets the CLI pick a default."
  (pm--run-async
   (pm--strs "agent" "ls"
             (and project (list "--project" project))
             (and all '("--all"))
             (and limit (list "-n" (number-to-string limit)))
             "--json")
   cb))

;;;; stacker
;;
;; Every `pm stacker' verb supports `--json' and returns the uniform
;; envelope `((ok . t) (command . VERB) (repo . R) (branch . B)
;; (message . MARKUP) (operation . OP-OR-NIL))'.  Each wrapper passes
;; `--repo' + the target branch explicitly so it is cwd-independent.

(defun pm--stacker-push (repo branch all draft publish cb)
  (pm--run-async
   (pm--strs "stacker" "push" branch "--repo" repo
             (and all '("--all"))
             (and draft '("--draft"))
             (and publish '("--publish"))
             "--json")
   cb :tag 'stacker-push))

(defun pm--stacker-sync (repo branch all offline drop-parent drop-merge cb)
  (pm--run-async
   (pm--strs "stacker" "sync" branch "--repo" repo
             (and all '("--all"))
             (and offline '("--offline"))
             (and drop-parent '("--allow-drop-parent-modifications"))
             (and drop-merge '("--allow-drop-merge"))
             "--json")
   cb :tag 'stacker-sync))

(defun pm--stacker-absorb (repo branch cb)
  (pm--run-async
   (pm--strs "stacker" "absorb" branch "--repo" repo "--json")
   cb :tag 'stacker-absorb))

(defun pm--stacker-remove (repo branch parent keep-branch force cb)
  (pm--run-async
   (pm--strs "stacker" "remove" branch "--repo" repo
             (and parent '("--parent"))
             (and keep-branch '("--keep-branch"))
             (and force '("--force"))
             "--json")
   cb :tag 'stacker-remove))

(defun pm--stacker-rename (repo branch new-name cb)
  (pm--run-async
   (pm--strs "stacker" "rename" new-name "--branch" branch "--repo" repo "--json")
   cb :tag 'stacker-rename))

(defun pm--stacker-reparent (repo branch new-parent offline drop-parent drop-merge cb)
  (pm--run-async
   (pm--strs "stacker" "reparent" new-parent "--branch" branch "--repo" repo
             (and offline '("--offline"))
             (and drop-parent '("--allow-drop-parent-modifications"))
             (and drop-merge '("--allow-drop-merge"))
             "--json")
   cb :tag 'stacker-reparent))

(defun pm--stacker-split (repo branch new-name commit stay cb)
  (pm--run-async
   (pm--strs "stacker" "split" new-name commit "--branch" branch "--repo" repo
             (and stay '("--stay"))
             "--json")
   cb :tag 'stacker-split))

(defun pm--stacker-create (repo new-branch on no-checkout cb)
  (pm--run-async
   (pm--strs "stacker" "create" new-branch "--repo" repo
             (and on (list "--on" on))
             (and no-checkout '("--no-checkout"))
             "--json")
   cb :tag 'stacker-create))

(defun pm--stacker-repair (repo branch base-ref cb)
  (pm--run-async
   (pm--strs "stacker" "repair" base-ref "--branch" branch "--repo" repo "--json")
   cb :tag 'stacker-repair))

(defun pm--stacker-continue (repo cb)
  (pm--run-async
   (pm--strs "stacker" "continue" "--repo" repo "--json")
   cb :tag 'stacker-continue))

(defun pm--stacker-abort (repo cb)
  (pm--run-async
   (pm--strs "stacker" "abort" "--repo" repo "--json")
   cb :tag 'stacker-abort))

(defun pm--stacker-pr-refresh (repo branch cb)
  (pm--run-async
   (pm--strs "stacker" "pr" "refresh" branch "--repo" repo "--json")
   cb :tag 'stacker-pr))

(defun pm--stacker-pr-unlink (repo branch all cb)
  (pm--run-async
   (pm--strs "stacker" "pr" "unlink"
             (if all '("--all") branch)
             "--repo" repo "--json")
   cb :tag 'stacker-pr))

(defun pm--stacker-log (repo branch cb)
  (pm--run-async
   (pm--strs "stacker" "log" branch "--repo" repo "--json")
   cb))

(provide 'pm-commands)

;;; pm-commands.el ends here
