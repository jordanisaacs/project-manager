;;; pm-status.el --- Magit-section status buffer for pm projects -*- lexical-binding: t -*-

;;; Commentary:

;; A magit-section buffer for `pm project status', built on
;; `magit-section-mode'.  Sections (Worktrees, PRs, Stacker, Sessions)
;; are foldable with TAB / S-TAB; movement keys (n/p, M-n/M-p) come from
;; `magit-section-mode-map'.  RET on a row dispatches based on the
;; section's `type' slot (a worktree row switches projects, a PR row
;; opens its URL, a session row hands off to `pm-agent--dispatch').
;;
;; Per-section refresh keys (`r w' / `r p' / `r s' / `r a') call
;; `pm project status -s <section> --json' and re-render only that
;; section, leaving the rest of the buffer (including fold state) in
;; place.  `g' refetches everything.

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'project)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)
(require 'pm-commands)
(require 'pm-ui)  ; for `pm--read-project'

;; Forward declaration — defined in pm-agent.el to keep the dispatch
;; defcustom alongside the agent-list buffer.  The autoload at the
;; bottom of pm.el guarantees it resolves before this RET handler runs.
(declare-function pm-agent--dispatch "pm-agent" (plist))

(defvar pm-projects-dir)

;;;; Buffer state

(defvar-local pm-status--project nil
  "Container name this status buffer is bound to.")

(defvar-local pm-status--data nil
  "Alist of (SECTION-SYMBOL . PARSED-JSON-LIST) for the rendered buffer.
Keys are `worktrees' / `prs' / `stacker' / `sessions' (symbols).
Updated by full and per-section refreshes; renderers read from here.")

;;;; Mode + keymap

(defvar pm-status-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (define-key map (kbd "g")     #'pm-status-refresh)
    (define-key map (kbd "RET")   #'pm-status-act-at-point)
    (define-key map (kbd "q")     #'quit-window)
    ;; Per-section refresh prefix.  The mnemonic mirrors the CLI flag
    ;; values: w=worktrees, p=prs, s=stacker, a=agent sessions.
    (define-key map (kbd "r w")   #'pm-status-refresh-worktrees)
    (define-key map (kbd "r p")   #'pm-status-refresh-prs)
    (define-key map (kbd "r s")   #'pm-status-refresh-stacker)
    (define-key map (kbd "r a")   #'pm-status-refresh-sessions)
    map)
  "Keymap for `pm-status-mode'.")

(define-derived-mode pm-status-mode magit-section-mode "PM Status"
  "Magit-style status buffer for a pm project.

\\{pm-status-mode-map}"
  (setq-local revert-buffer-function
              (lambda (&rest _) (pm-status-refresh))))

;;;; Section renderers

(defun pm-status--insert-worktrees ()
  "Render the Worktrees section from `pm-status--data'."
  (let* ((rows (alist-get 'worktrees pm-status--data))
         (groups (pm-table-group-by rows 'repo))
         (cell-rows
          (cons '("Worktree" "Kind" "Branch" "PR" "Stk" "Detail")
                (mapcar #'pm-status--worktree-cells rows)))
         (widths (pm-table-widths cell-rows)))
    (magit-insert-section (pm-worktrees nil)
      (magit-insert-heading
        (propertize (format "Worktrees (%d)" (length rows))
                    'face 'pm-section-heading))
      (insert (propertize (pm-table-row (car cell-rows) widths)
                          'face 'magit-section-heading))
      (insert "\n")
      (dolist (group groups)
        (let* ((repo (car group))
               (group-rows (cdr group)))
          (magit-insert-section (pm-worktree-repo repo)
            (magit-insert-heading
              (propertize (or repo "<no repo>") 'face 'pm-stacker-repo))
            (dolist (row group-rows)
              (let* ((cells (pm-status--worktree-cells row))
                     (kind-face (pm-faces-kind (alist-get 'kind row)))
                     (line (pm-table-row cells widths)))
                (magit-insert-section (pm-worktree row)
                  (insert (propertize line 'face kind-face))
                  (insert "\n")))))))
      (insert "\n"))))

(defun pm-status--worktree-cells (row)
  "Return display cells for a worktree ROW (alist from JSON)."
  (let* ((wt     (or (alist-get 'wt row) "—"))
         (kind   (or (alist-get 'kind row) ""))
         (branch (or (alist-get 'branch row) "—"))
         (pr     (alist-get 'pr row))
         (pr-cell (if pr "●" "-"))
         (tracked (if (eq (alist-get 'tracked row) t) "✓" ""))
         (detail (or (alist-get 'detail row) "")))
    (list wt kind branch pr-cell tracked detail)))

(defun pm-status--insert-prs ()
  "Render the PRs section from `pm-status--data'."
  (let* ((rows (alist-get 'prs pm-status--data)))
    (when rows
      (magit-insert-section (pm-prs nil)
        (magit-insert-heading
          (propertize (format "PRs (%d)" (length rows))
                      'face 'pm-section-heading))
        (let* ((cell-rows
                (cons '("WT" "Branch" "State" "URL")
                      (mapcar #'pm-status--pr-cells rows)))
               (widths (pm-table-widths cell-rows)))
          (insert (propertize (pm-table-row (car cell-rows) widths)
                              'face 'magit-section-heading))
          (insert "\n")
          (dolist (row rows)
            (let* ((cells (pm-status--pr-cells row))
                   (face (pm-faces-pr row)))
              (magit-insert-section (pm-pr row)
                (insert (propertize (pm-table-row cells widths)
                                    'face (or face 'default)))
                (insert "\n")))))
        (insert "\n")))))

(defun pm-status--pr-cells (row)
  "Return display cells for a PR ROW (alist from JSON)."
  (list (or (alist-get 'wt row) "")
        (or (alist-get 'branch row) "")
        (or (alist-get 'state row) "")
        (or (alist-get 'pr_url row) "")))

(defun pm-status--insert-stacker ()
  "Render the Stacker section from `pm-status--data'."
  (let ((rows (alist-get 'stacker pm-status--data)))
    (when rows
      (magit-insert-section (pm-stacker nil)
        (magit-insert-heading
          (propertize (format "Stacker (%d)" (length rows))
                      'face 'pm-section-heading))
        (dolist (row rows)
          (let ((repo (or (alist-get 'repo row) ""))
                (info (or (alist-get 'info row) "")))
            (magit-insert-section (pm-stacker-row row)
              (magit-insert-heading
                (propertize repo 'face 'pm-stacker-repo))
              (insert (pm-table-render-rich-markup info))
              (insert "\n"))))
        (insert "\n")))))

(defun pm-status--insert-sessions ()
  "Render the Recent Sessions section from `pm-status--data'."
  (let ((rows (alist-get 'sessions pm-status--data)))
    (magit-insert-section (pm-sessions nil)
      (magit-insert-heading
        (propertize (format "Recent sessions (%d)" (length rows))
                    'face 'pm-section-heading))
      (let* ((cell-rows
              (cons '("Agent" "Session" "Title" "Last Active")
                    (mapcar #'pm-status--session-cells rows)))
             (widths (pm-table-widths cell-rows)))
        (insert (propertize (pm-table-row (car cell-rows) widths)
                            'face 'magit-section-heading))
        (insert "\n")
        (dolist (row rows)
          (let* ((cells (pm-status--session-cells row))
                 (agent-face (pm-faces-agent (alist-get 'agent row))))
            (magit-insert-section (pm-session row)
              (insert
               (let ((line (pm-table-row cells widths)))
                 ;; Color just the Agent column; leave the rest in
                 ;; default so the title stays readable.
                 (let* ((agent-cell (car cells))
                        (after (substring line (length agent-cell))))
                   (concat (propertize agent-cell 'face agent-face)
                           after))))
              (insert "\n")))))
      (insert "\n"))))

(defun pm-status--session-cells (row)
  "Return display cells for a session ROW (alist from JSON)."
  (let* ((title (or (alist-get 'title row) ""))
         (truncated (if (> (length title) 70)
                        (concat (substring title 0 69) "…")
                      title))
         (last (alist-get 'last_active row)))
    (list (or (alist-get 'agent row) "")
          (or (alist-get 'session_id row) "")
          truncated
          (or (and last (pm-table-format-iso last)) ""))))

;;;; Buffer population

(defun pm-status--render ()
  "Populate the current buffer from `pm-status--data'.

The body sits inside a single root `pm-status' section so every
char carries the `magit-section' text property —
`magit-section-update-highlight' would otherwise iterate stale
sections from the previous render and trip with a nil section, so
we also reset its bookkeeping (`pm-table-reset-section-state')
before re-rendering, mirroring `magit-refresh-buffer'."
  (let ((inhibit-read-only t)
        (line (line-number-at-pos)))
    (pm-table-reset-section-state)
    (erase-buffer)
    (magit-insert-section (pm-status pm-status--project)
      (magit-insert-heading
        (propertize (format "pm: %s" pm-status--project) 'face 'bold))
      (insert "\n")
      (pm-status--insert-worktrees)
      (pm-status--insert-prs)
      (pm-status--insert-stacker)
      (pm-status--insert-sessions))
    (pm-table-cover-root-section)
    (goto-char (point-min))
    (forward-line (1- line))))

;;;; Refresh commands

;;;###autoload
(defun pm-status-refresh ()
  "Refetch the entire status buffer (`pm project status --json')."
  (interactive)
  (unless pm-status--project
    (user-error "Not in a pm-status buffer"))
  (let ((buf (current-buffer))
        (project pm-status--project))
    (pm--project-status
     project
     (lambda (data)
       (when (buffer-live-p buf)
         (with-current-buffer buf
           (setq pm-status--data
                 (list (cons 'worktrees (alist-get 'worktrees data))
                       (cons 'prs       (alist-get 'prs data))
                       (cons 'stacker   (alist-get 'stacker data))
                       (cons 'sessions  (alist-get 'sessions data))))
           (pm-status--render)))))))

(defun pm-status--refresh-section (section)
  "Refetch SECTION (a symbol) and re-render the whole buffer."
  (unless pm-status--project
    (user-error "Not in a pm-status buffer"))
  (let ((buf (current-buffer))
        (project pm-status--project)
        (key (symbol-name section)))
    (pm--project-status-section
     key project
     (lambda (data)
       (when (buffer-live-p buf)
         (with-current-buffer buf
           (let ((entry (assq section pm-status--data)))
             (if entry
                 (setcdr entry (alist-get section data))
               (push (cons section (alist-get section data))
                     pm-status--data)))
           (pm-status--render)
           (message "pm: refreshed %s" key)))))))

(defun pm-status-refresh-worktrees ()
  "Refetch only the Worktrees section."
  (interactive) (pm-status--refresh-section 'worktrees))

(defun pm-status-refresh-prs ()
  "Refetch only the PRs section."
  (interactive) (pm-status--refresh-section 'prs))

(defun pm-status-refresh-stacker ()
  "Refetch only the Stacker section."
  (interactive) (pm-status--refresh-section 'stacker))

(defun pm-status-refresh-sessions ()
  "Refetch only the Sessions section."
  (interactive) (pm-status--refresh-section 'sessions))

;;;; RET dispatch

(defun pm-status-act-at-point ()
  "Run the section-specific action for the row at point."
  (interactive)
  (let* ((sec (magit-current-section))
         (target (pm-section-ancestor-of-type
                  sec '(pm-worktree pm-pr pm-session))))
    (unless target
      (user-error "No actionable row at point"))
    (let ((value (oref target value)))
      (pcase (oref target type)
        ('pm-worktree
         (let* ((wt (alist-get 'wt value))
                (forward (alist-get 'forward_path value))
                (target-dir
                 (or (and forward (file-directory-p forward) forward)
                     (and wt
                          (expand-file-name
                           wt
                           (expand-file-name
                            pm-status--project pm-projects-dir))))))
           (unless (and target-dir (file-directory-p target-dir))
             (user-error "Worktree path not on disk: %s" target-dir))
           (project-switch-project target-dir)))
        ('pm-pr
         (let ((url (alist-get 'pr_url value)))
           (unless url (user-error "No URL on this PR row"))
           (browse-url url)))
        ('pm-session
         (pm-agent--dispatch
          (list :agent (alist-get 'agent value)
                :session-id (alist-get 'session_id value)
                :project (or (alist-get 'project value)
                             pm-status--project))))))))

;;;; Entry point

;;;###autoload
(defun pm-project-status (name)
  "Show `pm project status NAME' in a magit-style buffer."
  (interactive (list (pm--read-project "Status of: ")))
  (let ((buf (get-buffer-create (format "*pm-status: %s*" name))))
    (with-current-buffer buf
      (pm-status-mode)
      (setq pm-status--project name)
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert (format "pm: %s\n\nLoading…\n" name)))
      (pm-status-refresh))
    (pop-to-buffer buf)))

(provide 'pm-status)

;;; pm-status.el ends here
