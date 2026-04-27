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

(defun pm--project-create (name spec cb)
  (pm--run-async
   (pm--strs "project" "create" name
             (and spec (list "--wt" spec))
             "--json")
   cb :tag 'create))

(defun pm--project-delete (name repos cb)
  (pm--run-async
   (pm--strs "project" "delete" name
             (and repos (list "--repos" repos))
             "--json")
   cb :tag 'delete))

;;;; project wt

(defun pm--wt-create (name spec cb)
  (pm--run-async
   (pm--strs "project" "wt" "create" name "--wt" spec "--json")
   cb :tag 'wt-create))

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

(defun pm--wt-delete (name wts all cb)
  (pm--run-async
   (pm--strs "project" "wt" "delete" name
             (cond (all '("--all"))
                   (wts (list "--wt" wts)))
             "--json")
   cb :tag 'wt-delete))

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

(provide 'pm-commands)

;;; pm-commands.el ends here
