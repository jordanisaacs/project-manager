;;; pm-stacker.el --- Interactive stacker tree + actions -*- lexical-binding: t -*-

;;; Commentary:

;; A magit-section buffer for the stacker: one foldable branch tree per
;; tracked repo in a pm container, built from the structured tree that
;; `pm project status -s stacker --json' now emits (`StackerRow.branches').
;;
;; The tree renderer (`pm-stacker--insert-repos') is shared with the
;; `*pm-status:*' Stacker section so both surfaces are identical and
;; interactive.  RET on a branch opens a branch-scoped action transient
;; (`pm-stacker-dispatch'); direct keys run the common verbs against the
;; branch at point.  Every action flows through a `pm stacker <verb>
;; --json' wrapper (see pm-commands.el) and refreshes the buffer, so a
;; paused/conflicted operation surfaces in the per-repo banner with
;; continue/abort one key away.

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)
(require 'pm-commands)
(require 'pm-ui)  ; for `pm--read-project' / `pm--maybe-confirm'

(defvar pm-projects-dir)
(defvar pm-status--project)  ; set in `*pm-status:*' buffers; read as a fallback

(declare-function transient-setup "transient" (&optional name layout params &rest args))
(declare-function pm-stacker-dispatch "pm-transient" (&optional scope))
(declare-function magit-section-highlight-range "magit-section" (start end &optional face))
(defvar magit--section-type-alist)

;;;; Branch section class
;;
;; Branch rows nest their descendants (so subtree folding works), but
;; magit's default highlight covers a section's whole region — which
;; would light up a parent's entire arm.  A dedicated section class lets
;; us override `magit-section-highlight' to cover only the heading line,
;; so a branch row highlights as a single line while the enclosing repo
;; section still highlights as a block (table-style).  Registering the
;; class under the `pm-stacker-branch' type keeps `oref … type' (and
;; thus `pm-section-ancestor-of-type') working as before.

(defclass pm-stacker-branch-section (magit-section) ()
  "Section class for a single stacker branch row.")

(with-eval-after-load 'magit-section
  (add-to-list 'magit--section-type-alist
               '(pm-stacker-branch . pm-stacker-branch-section)))

(cl-defmethod magit-section-highlight ((section pm-stacker-branch-section))
  "Highlight only SECTION's own heading line, not its nested children."
  (magit-section-highlight-range
   (oref section start)
   (or (oref section content) (oref section end))))

;;;; Buffer state

(defvar-local pm-stacker--project nil
  "Container name this stacker buffer is bound to.")

(defvar-local pm-stacker--data nil
  "Last `stacker' section payload: a list of per-repo rows.")

(defvar-local pm-stacker--wt-map nil
  "Alist mapping (REPO . BRANCH) to the worktree path checked out there.
Populated from the `worktrees' section so `w' can visit a branch.")

;;;; Tree renderer (shared with pm-status)

(defun pm-stacker--node-markers (node)
  "Return a propertized marker string for NODE, or \"\" when none apply.

Mirrors the CLI's commit-group suffix: `(N)' commit count (dim),
`!' needs-sync (warn), `↑N' commits ahead of remote (warn)."
  (let ((cc (alist-get 'commit_count node))
        (ahead (alist-get 'ahead_of_remote node))
        (needs (alist-get 'needs_sync node))
        parts)
    (when (and (integerp cc) (> cc 0))
      (push (pm-propertize-face (format "(%d)" cc) 'pm-dim) parts))
    (when needs
      (push (pm-propertize-face "!" 'pm-row-warn) parts))
    (when (and (integerp ahead) (> ahead 0))
      (push (pm-propertize-face (format "↑%d" ahead) 'pm-row-warn) parts))
    (if parts (concat " " (string-join (nreverse parts) " ")) "")))

(defun pm-stacker--insert-node (node current depth)
  "Insert NODE (and its children) at tree DEPTH.

CURRENT is the repo's `current_branch' (or nil).  Each branch is a
foldable `pm-stacker-branch' section that nests its children, so
`TAB' collapses a subtree; the section's custom class
(`pm-stacker-branch-section') overrides highlighting so the row reads
as a single line.  Status icon + branch name are themed, the
merged/current rows styled distinctly."
  (let* ((branch (or (alist-get 'branch node) ""))
         (icon-face (pm-faces-stacker-status (alist-get 'status node)))
         (icon (car icon-face))
         (iface (or (cdr icon-face) 'default))
         (merged (eq t (alist-get 'merged node)))
         (is-current (and current (string= branch current)))
         (name-face (cond (merged 'pm-stacker-merged)
                          (is-current 'pm-stacker-current)
                          (t 'default)))
         (indent (make-string (* depth 2) ?\s)))
    (magit-insert-section (pm-stacker-branch node)
      (magit-insert-heading
        (concat indent
                (pm-propertize-face icon iface)
                " "
                (pm-propertize-face branch name-face)
                (pm-stacker--node-markers node)))
      (dolist (child (alist-get 'children node))
        (pm-stacker--insert-node child current (1+ depth))))))

(defun pm-stacker--insert-operation (op)
  "Insert a one-line paused-operation banner for OP (an alist)."
  (let ((type (or (alist-get 'op_type op) "?"))
        (status (or (alist-get 'status op) ""))
        (err (alist-get 'error_message op)))
    (insert (pm-propertize-face
             (format "⚠ %s %s%s" type status
                     (if err (format " — %s" err) ""))
             'pm-row-warn))
    (insert "\n")))

(defun pm-stacker--insert-repos (rows)
  "Insert the per-repo stacker trees for ROWS.

ROWS is the `stacker' section payload — a list of alists with
`repo', `branches', `current_branch', `operation'.  Must run inside
an enclosing `magit-insert-section' (the caller's root)."
  (if (null rows)
      (pm-table-insert-empty "(no tracked branches)")
    (dolist (repo-row rows)
      (let ((repo (or (alist-get 'repo repo-row) ""))
            (branches (alist-get 'branches repo-row))
            (current (alist-get 'current_branch repo-row))
            (op (alist-get 'operation repo-row)))
        (magit-insert-section (pm-stacker-repo repo-row)
          (magit-insert-heading (pm-propertize-face repo 'pm-group-heading))
          (when op (pm-stacker--insert-operation op))
          (if (null branches)
              (pm-table-insert-empty "(no tracked branches)")
            (dolist (node branches)
              (pm-stacker--insert-node node current 0)))
          (insert "\n"))))))

;;;; Buffer mode

(defvar pm-stacker-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (define-key map (kbd "g")   #'pm-stacker-refresh)
    (define-key map (kbd "RET") #'pm-stacker-act-at-point)
    (define-key map (kbd "w")   #'pm-stacker-visit-worktree)
    (define-key map (kbd "b")   #'pm-stacker-browse-pr)
    (define-key map (kbd "L")   #'pm-stacker-log-at-point)
    ;; Direct one-key actions on the branch at point.
    (define-key map (kbd "p")   #'pm-stacker-push)
    (define-key map (kbd "y")   #'pm-stacker-sync)
    (define-key map (kbd "a")   #'pm-stacker-absorb)
    (define-key map (kbd "c")   #'pm-stacker-create)
    (define-key map (kbd "r")   #'pm-stacker-rename)
    (define-key map (kbd "R")   #'pm-stacker-reparent)
    (define-key map (kbd "S")   #'pm-stacker-split)
    (define-key map (kbd "x")   #'pm-stacker-remove)
    (define-key map (kbd "P")   #'pm-stacker-pr-refresh)
    (define-key map (kbd "U")   #'pm-stacker-pr-unlink)
    (define-key map (kbd "C")   #'pm-stacker-continue)
    (define-key map (kbd "A")   #'pm-stacker-abort)
    (define-key map (kbd "F")   #'pm-stacker-repair)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `pm-stacker-mode'.")

(define-derived-mode pm-stacker-mode magit-section-mode "PM Stacker"
  "Magit-style interactive stacker tree for a pm project.

\\{pm-stacker-mode-map}"
  (setq-local revert-buffer-function (lambda (&rest _) (pm-stacker-refresh)))
  (pm-section-setup-margin))

(defun pm-stacker--render ()
  "Populate the current buffer from `pm-stacker--data'."
  (let ((inhibit-read-only t)
        (rows pm-stacker--data)
        (line (line-number-at-pos)))
    (pm-table-reset-section-state)
    (erase-buffer)
    (magit-insert-section (pm-stacker pm-stacker--project)
      (insert (pm-table-banner (format "stacker: %s" pm-stacker--project)
                               (length rows)))
      (insert "\n\n")
      (pm-stacker--insert-repos rows))
    (pm-table-cover-root-section)
    (pm-table-show-root-section)
    (goto-char (point-min))
    (forward-line (1- line))))

(defun pm-stacker--build-wt-map (wts)
  "Build a (REPO . BRANCH) → path alist from worktree section rows WTS."
  (let (map)
    (dolist (wt wts)
      (let ((repo (alist-get 'repo wt))
            (branch (alist-get 'branch wt))
            (forward (alist-get 'forward_path wt))
            (name (alist-get 'wt wt)))
        (when (and repo branch)
          (push (cons (cons repo branch)
                      (or forward
                          (and name pm-stacker--project
                               (expand-file-name
                                name (expand-file-name pm-stacker--project
                                                       pm-projects-dir)))))
                map))))
    map))

(defun pm-stacker-refresh ()
  "Refetch the stacker tree (and worktree paths) and re-render."
  (interactive)
  (unless pm-stacker--project
    (user-error "Not in a pm-stacker buffer"))
  (let ((buf (current-buffer))
        (name pm-stacker--project))
    (pm--project-status-section
     "worktrees,stacker" name
     (lambda (data)
       (when (buffer-live-p buf)
         (with-current-buffer buf
           (setq pm-stacker--data (alist-get 'stacker data))
           (setq pm-stacker--wt-map
                 (pm-stacker--build-wt-map (alist-get 'worktrees data)))
           (pm-stacker--render)))))))

;;;; Target resolution

(defun pm-stacker--project-name ()
  "Resolve the container name for the current buffer.
Works in both `*pm-stacker:*' and `*pm-status:*' buffers."
  (or (and (bound-and-true-p pm-stacker--project) pm-stacker--project)
      (and (bound-and-true-p pm-status--project) pm-status--project)))

(defun pm-stacker--target-at-point ()
  "Return a plist (:project :repo :branch :node) for the branch at point."
  (let* ((sec (magit-current-section))
         (target (and sec (pm-section-ancestor-of-type sec '(pm-stacker-branch)))))
    (unless target
      (user-error "No stacker branch at point"))
    (let ((node (oref target value)))
      (list :project (pm-stacker--project-name)
            :repo (alist-get 'repo_name node)
            :branch (alist-get 'branch node)
            :node node))))

;;;; Action plumbing

(defun pm-stacker--after-action (buf)
  "Return a completion callback that reports the envelope and refreshes BUF."
  (lambda (env)
    (let* ((msg (and env (alist-get 'message env)))
           (op (and env (alist-get 'operation env)))
           (paused (and op (member (alist-get 'status op) '("paused" "PAUSED"))))
           (rendered (and msg (not (string-empty-p msg))
                          (string-trim (pm-table-render-rich-markup msg)))))
      (cond
       (paused
        (message "pm stacker: %s paused — resolve, then C continue / A abort"
                 (alist-get 'op_type op)))
       ((and rendered (not (string-empty-p rendered)))
        (message "%s" rendered)))
      ;; Refresh whichever pm magit-section buffer issued the action — the
      ;; mode's `revert-buffer-function' routes to the right refresh, so the
      ;; re-fetched data (incl. operation state) repaints the banner.
      (when (buffer-live-p buf)
        (with-current-buffer buf
          (when (derived-mode-p 'magit-section-mode)
            (revert-buffer nil t)))))))

(defun pm-stacker--run (thunk &optional confirm-prompt)
  "Run THUNK (a 1-arg fn taking the after-action callback).
When CONFIRM-PROMPT is non-nil, gate on `pm--maybe-confirm' first."
  (when (or (null confirm-prompt) (pm--maybe-confirm confirm-prompt))
    (funcall thunk (pm-stacker--after-action (current-buffer)))))

;;;; Interactive actions (operate on the branch at point)

;;;###autoload
(defun pm-stacker-act-at-point ()
  "Open the branch-scoped stacker action transient for the branch at point."
  (interactive)
  (require 'pm-transient)  ; defines `pm-stacker-dispatch'
  (let ((tgt (pm-stacker--target-at-point)))
    (transient-setup 'pm-stacker-dispatch nil nil
                     :scope (list :project (plist-get tgt :project)
                                  :repo (plist-get tgt :repo)
                                  :branch (plist-get tgt :branch)))))

(defun pm-stacker-push ()
  "Force-push + create/update the PR for the branch at point."
  (interactive)
  (let ((tgt (pm-stacker--target-at-point)))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-push (plist-get tgt :repo) (plist-get tgt :branch)
                         nil nil nil cb)))))

(defun pm-stacker-sync ()
  "Cherry-pick the branch at point onto its parent."
  (interactive)
  (let ((tgt (pm-stacker--target-at-point)))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-sync (plist-get tgt :repo) (plist-get tgt :branch)
                         nil nil nil nil cb)))))

(defun pm-stacker-absorb ()
  "Absorb the branch at point's commits into its parent."
  (interactive)
  (let ((tgt (pm-stacker--target-at-point)))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-absorb (plist-get tgt :repo) (plist-get tgt :branch) cb)))))

(defun pm-stacker-create ()
  "Create a new tracked branch on top of the branch at point."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (parent (plist-get tgt :branch))
         (new (read-string "New branch name: "))
         (on (read-string "On parent: " parent)))
    (when (string-empty-p new) (user-error "No branch name"))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-create (plist-get tgt :repo) new
                           (and (not (string-empty-p on)) on) nil cb)))))

(defun pm-stacker-rename ()
  "Rename the branch at point."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (branch (plist-get tgt :branch))
         (new (read-string (format "Rename %s to: " branch))))
    (when (string-empty-p new) (user-error "No new name"))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-rename (plist-get tgt :repo) branch new cb)))))

(defun pm-stacker-reparent ()
  "Reparent the branch at point onto a new parent."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (branch (plist-get tgt :branch))
         (parent (read-string (format "Reparent %s onto: " branch))))
    (when (string-empty-p parent) (user-error "No parent"))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-reparent (plist-get tgt :repo) branch parent nil nil nil cb))
     (format "Reparent %s onto %s? " branch parent))))

(defun pm-stacker-split ()
  "Split commits [<commit>..HEAD] of the branch at point onto a new child."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (branch (plist-get tgt :branch))
         (new (read-string "New child branch name: "))
         (commit (read-string "Split at commit: ")))
    (when (or (string-empty-p new) (string-empty-p commit))
      (user-error "Need both a name and a commit"))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-split (plist-get tgt :repo) branch new commit nil cb))
     (format "Split %s at %s onto %s? " branch commit new))))

(defun pm-stacker-remove ()
  "Untrack (and delete) the branch at point."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (branch (plist-get tgt :branch)))
    (pm-stacker--run
     (lambda (cb)
       ;; `--force' skips the CLI's own prompt; we confirm in Emacs instead.
       (pm--stacker-remove (plist-get tgt :repo) branch nil nil t cb))
     (format "Remove tracked branch %s? " branch))))

(defun pm-stacker-repair ()
  "Reset the branch at point's managed base to a ref."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (branch (plist-get tgt :branch))
         (base (read-string (format "Repair %s managed-base to ref: " branch))))
    (when (string-empty-p base) (user-error "No base ref"))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-repair (plist-get tgt :repo) branch base cb))
     (format "Repair %s base to %s? " branch base))))

(defun pm-stacker-continue ()
  "Resume the paused operation in the repo at point."
  (interactive)
  (let ((tgt (pm-stacker--target-at-point)))
    (pm-stacker--run
     (lambda (cb) (pm--stacker-continue (plist-get tgt :repo) cb)))))

(defun pm-stacker-abort ()
  "Abort the paused operation in the repo at point."
  (interactive)
  (let ((tgt (pm-stacker--target-at-point)))
    (pm-stacker--run
     (lambda (cb) (pm--stacker-abort (plist-get tgt :repo) cb))
     "Abort the paused operation? ")))

(defun pm-stacker-pr-refresh ()
  "Re-discover and cache the GitHub PR for the branch at point."
  (interactive)
  (let ((tgt (pm-stacker--target-at-point)))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-pr-refresh (plist-get tgt :repo) (plist-get tgt :branch) cb)))))

(defun pm-stacker-pr-unlink ()
  "Clear the cached PR association for the branch at point."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (branch (plist-get tgt :branch)))
    (pm-stacker--run
     (lambda (cb)
       (pm--stacker-pr-unlink (plist-get tgt :repo) branch nil cb))
     (format "Clear PR link for %s? " branch))))

(defun pm-stacker-log-at-point ()
  "Show commits since the managed base for the branch at point."
  (interactive)
  (let ((tgt (pm-stacker--target-at-point)))
    (pm--stacker-log
     (plist-get tgt :repo) (plist-get tgt :branch)
     (lambda (env)
       (let ((msg (or (and env (alist-get 'message env)) "")))
         (with-current-buffer (get-buffer-create "*pm-stacker-log*")
           (let ((inhibit-read-only t))
             (erase-buffer)
             (insert (pm-table-render-rich-markup msg))
             (goto-char (point-min)))
           (special-mode)
           (display-buffer (current-buffer))))))))

(defun pm-stacker-visit-worktree ()
  "Switch to the worktree checked out for the branch at point.
Falls back to opening the PR when the branch isn't checked out."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (node (plist-get tgt :node))
         (path (cdr (assoc (cons (plist-get tgt :repo) (plist-get tgt :branch))
                           pm-stacker--wt-map))))
    (cond
     ((and path (file-directory-p path)) (project-switch-project path))
     ((alist-get 'pr_url node) (browse-url (alist-get 'pr_url node)))
     (t (user-error "%s is not checked out and has no PR"
                    (plist-get tgt :branch))))))

(defun pm-stacker-browse-pr ()
  "Open the PR for the branch at point in a browser."
  (interactive)
  (let* ((tgt (pm-stacker--target-at-point))
         (url (alist-get 'pr_url (plist-get tgt :node))))
    (unless url (user-error "No PR for %s" (plist-get tgt :branch)))
    (browse-url url)))

;;;; Entry point

;;;###autoload
(defun pm-stacker-list (&optional project)
  "Open the interactive stacker tree for pm PROJECT (prompts when nil)."
  (interactive (list (pm--read-project "Stacker for: ")))
  (let* ((name (or project (pm--read-project "Stacker for: ")))
         (buf (get-buffer-create (format "*pm-stacker: %s*" name))))
    (with-current-buffer buf
      (pm-stacker-mode)
      (setq pm-stacker--project name)
      (setq default-directory
            (file-name-as-directory (expand-file-name name pm-projects-dir)))
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "Loading…\n"))
      (pm-stacker-refresh))
    (pop-to-buffer buf)
    (pm-section-set-window-margin)))

(provide 'pm-stacker)

;;; pm-stacker.el ends here
