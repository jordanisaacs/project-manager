;;; pm-agent.el --- Coding-agent session list with resume dispatch -*- lexical-binding: t -*-

;;; Commentary:

;; A magit-section buffer for `pm agent ls', plus a thin
;; `pm-agent-launch' entry point for fresh `pm agent <name>'
;; invocations.  RET on a session row resumes; `pm-agent-launch'
;; spawns a new session.  Both flow through one customizable
;; dispatch function (`pm-agent-dispatch-function'), which receives
;; a plist describing what to launch:
;;
;;   (:argv ("pm" "agent" "claude" "--project" "kms" "--resume" "<id>")
;;    :cwd  "/home/.../.projects/kms/"
;;    :agent      "claude"
;;    :session-id "..."   ; nil for a fresh launch
;;    :project    "kms")
;;
;; Three opt-in dispatchers ship with this file:
;;   - `pm-agent-dispatch-term'    (built-in `term-mode')
;;   - `pm-agent-dispatch-vterm'   (requires the `vterm' package)
;;   - `pm-agent-dispatch-ghostel' (requires the `ghostel' package)
;;
;; Default behavior (`pm-agent-dispatch-function' = nil) stages the
;; command on the kill-ring and prints a hint, so the package is
;; harmless out of the box.

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)
(require 'pm-commands)
(require 'pm-project)  ; for `pm--container-of'
(require 'pm-ui)  ; for `pm--read-project'

(defvar pm-projects-dir)
(defvar pm-executable)

(defvar vterm-buffer-name)  ; declared dynamic so the let-binding takes effect
(declare-function vterm "ext:vterm" (&optional buffer-name))
(declare-function vterm-send-string "ext:vterm" (string &optional paste-p))
(declare-function vterm-send-return "ext:vterm" ())
(declare-function term-mode "term" ())
(declare-function term-char-mode "term" ())

(defvar ghostel-buffer-name)  ; declared dynamic so the let-binding takes effect
(declare-function ghostel "ext:ghostel" (&optional arg))
(declare-function ghostel-paste-string "ext:ghostel" (string))
(declare-function ghostel-send-key "ext:ghostel" (key-name &optional mods))

;;;; Dispatch defcustom

;;;###autoload
(defcustom pm-agent-dispatch-function nil
  "Function called to launch a resumed agent.

Receives one plist argument: \(:argv :cwd :agent :session-id :project\).
When nil (the default), RET on a session row stages the command on
the kill-ring and prints a message instead of launching anything.

Set to one of the built-in dispatchers shipped with this file:
  - `pm-agent-dispatch-term'    (uses Emacs's built-in `term-mode')
  - `pm-agent-dispatch-vterm'   (requires the `vterm' package)
  - `pm-agent-dispatch-ghostel' (requires the `ghostel' package)
or write your own — for example, to spawn an external terminal
emulator."
  :type '(choice (const  :tag "Disabled (kill-ring stage)" nil)
                 (function :tag "Custom dispatcher"))
  :group 'pm)

;;;; Dispatch implementation

(defun pm-agent--build-argv (agent session-id project)
  "Build the argv list for AGENT scoped to PROJECT.

When SESSION-ID is non-nil, builds a resume invocation; when nil,
builds a fresh-launch invocation (`pm agent AGENT --project
PROJECT', no resume bits).  The fresh-launch form is the entry
point for `pm-agent-launch'.

Mirrors the per-agent resume invocation form in
`src/project_manager/agent/run.py' (`_RESUME_PREFIX'):
  claude/cursor → `--resume <id>'
  codex         → `resume <id>'"
  (let* ((bin (or (and (boundp 'pm-executable) pm-executable) "pm"))
         (resume-prefix
          (and session-id
               (pcase agent
                 ("codex" '("resume"))
                 (_       '("--resume"))))))
    (append (list bin "agent" agent "--project" project)
            resume-prefix
            (and session-id (list session-id)))))

(defun pm-agent--cwd (project)
  "Resolve PROJECT's container path on disk."
  (file-name-as-directory
   (expand-file-name project pm-projects-dir)))

(defun pm-agent--dispatch (plist)
  "Launch the agent described by PLIST, or stage it if disabled.

PLIST keys: `:agent', `:session-id', `:project'.  Builds `:argv'
and `:cwd', then either funcalls `pm-agent-dispatch-function' or
falls through to the kill-ring stage."
  (let* ((agent      (plist-get plist :agent))
         (session-id (plist-get plist :session-id))
         (project    (plist-get plist :project))
         (argv       (pm-agent--build-argv agent session-id project))
         (cwd        (pm-agent--cwd project))
         (full       (append plist (list :argv argv :cwd cwd))))
    (cond
     ((functionp pm-agent-dispatch-function)
      (funcall pm-agent-dispatch-function full))
     (t
      (let ((cmd (mapconcat #'shell-quote-argument argv " ")))
        (kill-new cmd)
        (message
         "pm: command on kill-ring (set `pm-agent-dispatch-function' to launch): %s"
         cmd))))))

;;;; Built-in dispatchers (opt-in)

;;;###autoload
(defun pm-agent-dispatch-term (plist)
  "Launch PLIST's `:argv' inside a built-in `term-mode' buffer.

PLIST is the dispatch payload built by `pm-agent--dispatch'.  The
buffer is named after the agent + project so multiple resumes
coexist."
  (require 'term)
  (let* ((argv (plist-get plist :argv))
         (cwd  (plist-get plist :cwd))
         (agent (plist-get plist :agent))
         (project (plist-get plist :project))
         (name (format "pm-agent: %s/%s" project agent))
         (default-directory cwd))
    ;; `make-term' takes (NAME PROGRAM &optional STARTFILE &rest ARGS).
    ;; We let argv[0] be the program and pass the rest as args.
    (let ((buf (apply #'make-term name (car argv) nil (cdr argv))))
      (with-current-buffer buf
        (term-mode)
        (term-char-mode))
      (pop-to-buffer buf))))

;;;###autoload
(defun pm-agent-dispatch-vterm (plist)
  "Launch PLIST's `:argv' inside a `vterm' buffer.

Requires the `vterm' package; raises `user-error' otherwise so
users get a clear hint instead of a void-function backtrace."
  (unless (require 'vterm nil t)
    (user-error "vterm not installed; pick another `pm-agent-dispatch-function'"))
  (let* ((argv (plist-get plist :argv))
         (cwd  (plist-get plist :cwd))
         (agent (plist-get plist :agent))
         (project (plist-get plist :project))
         (default-directory cwd)
         (buffer-name (format "*pm-agent: %s/%s*" project agent)))
    (let ((vterm-buffer-name buffer-name))
      (vterm))
    (vterm-send-string (mapconcat #'shell-quote-argument argv " "))
    (vterm-send-return)))

;;;###autoload
(defun pm-agent-dispatch-ghostel (plist)
  "Launch PLIST's `:argv' inside a `ghostel' buffer.

Requires the `ghostel' package; raises `user-error' otherwise so
users get a clear hint instead of a void-function backtrace.

The argv is shell-quoted and forwarded via `ghostel-paste-string'
(bracketed paste — the shell treats it as one atomic input even
when it contains spaces or quotes), then a Return key is sent to
execute it.  Bracketed paste falls back to a raw write before the
shell has set DECSET 2004; either way the shell receives the
argv as a single command line.

A fresh ghostel buffer is always allocated (via `generate-new-buffer'
under the hood); repeated launches of the same agent/project get
`<2>', `<3>', ... suffixes rather than pasting into a live session."
  (unless (require 'ghostel nil t)
    (user-error "ghostel not installed; pick another `pm-agent-dispatch-function'"))
  (let* ((argv    (plist-get plist :argv))
         (cwd     (plist-get plist :cwd))
         (agent   (plist-get plist :agent))
         (project (plist-get plist :project))
         (default-directory cwd)
         (ghostel-buffer-name (format "*pm-agent: %s/%s*" project agent))
         ;; Non-numeric, non-nil prefix arg → ghostel branches to
         ;; `generate-new-buffer', so we never paste into an existing
         ;; agent session by reusing its buffer name.
         (buf (ghostel t)))
    (with-current-buffer buf
      (ghostel-paste-string (mapconcat #'shell-quote-argument argv " "))
      (ghostel-send-key "return"))))

;;;; Buffer + mode

(defun pm-agent-list--buffer-name (project all)
  "Return the agent-list buffer name for scope PROJECT / ALL.

The all-projects view keeps the bare `*pm-agent-ls*' name.  A
single-project view is suffixed with the project name so each
per-project list gets its own buffer instead of clobbering the
all-projects view (and the other per-project lists)."
  (if (and project (not all))
      (format "*pm-agent-ls: %s*" project)
    "*pm-agent-ls*"))

(defvar-local pm-agent-list--scope nil
  "Cons (PROJECT . ALL-FLAG) the current `*pm-agent-ls*' buffer was opened with.
PROJECT is a project name string or nil; ALL-FLAG is t to force the
all-projects view.  Used by `pm-agent-list-refresh'.")

(defvar-local pm-agent-list--data nil
  "Last parsed `pm agent ls --json' result for this buffer.")

(defvar pm-agent-list-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)
    (define-key map (kbd "g")   #'pm-agent-list-refresh)
    (define-key map (kbd "RET") #'pm-agent-list-act-at-point)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `pm-agent-list-mode'.")

(define-derived-mode pm-agent-list-mode magit-section-mode "PM Agent"
  "Magit-style buffer listing recent agent sessions across pm projects.

\\{pm-agent-list-mode-map}"
  (setq-local revert-buffer-function
              (lambda (&rest _) (pm-agent-list-refresh)))
  (pm-section-setup-margin))

(defun pm-agent-list--render ()
  "Populate the current buffer from `pm-agent-list--data'.

Wrapped in a single root `pm-agent-list' section so every char in
the buffer carries the `magit-section' text property — required by
magit-section's post-command hook."
  (let ((inhibit-read-only t)
        (rows pm-agent-list--data)
        (line (line-number-at-pos)))
    (pm-table-reset-section-state)
    (erase-buffer)
    (magit-insert-section (pm-agent-list nil)
      ;; Root has no heading — see pm-status for the rationale.
      (insert (pm-table-banner "Recent agent sessions" (length rows)))
      (insert "\n")
      (if (null rows)
          (pm-table-insert-empty)
        (let* ((groups (pm-agent-list--group-by-project rows))
               (header '("Agent" "Session" "Title" "Last Active"))
               (cell-rows (cons header (mapcar #'pm-agent-list--cells rows)))
               (widths (pm-table-widths cell-rows)))
          (pm-table-insert-column-header header widths)
          (dolist (group groups)
            (let ((project (car group))
                  (group-rows (cdr group)))
              (magit-insert-section (pm-agent-project project)
                (magit-insert-heading
                  (pm-propertize-face project 'pm-group-heading))
                (dolist (row group-rows)
                  (let ((cells (pm-agent-list--cells row))
                        (faces (pm-faces-session-cells row)))
                    (magit-insert-section (pm-session row)
                      (insert (pm-table-row-faced cells widths faces))
                      (insert "\n"))))))))))
    (pm-table-cover-root-section)
    (pm-table-show-root-section)
    (goto-char (point-min))
    (forward-line (1- line))))

(defun pm-agent-list--cells (row)
  "Return display cells for an agent-ls ROW (alist from JSON)."
  (let* ((title (or (alist-get 'title row) ""))
         (truncated (if (> (length title) 70)
                        (concat (substring title 0 69) "…")
                      title))
         (last (alist-get 'last_active row)))
    (list (or (alist-get 'agent row) "")
          (or (alist-get 'session_id row) "")
          truncated
          (or (and last (pm-table-format-iso last)) ""))))

(defun pm-agent-list--group-by-project (rows)
  "Group ROWS by `project' field, preserving server-provided order."
  (pm-table-group-by rows 'project))

(defun pm-agent-list--flatten-groups (groups)
  "Flatten `pm agent ls --json' output into a list of session rows.

The CLI emits a sectioned payload: a top-level array of
\(project, sessions\) groups (see `JsonShape(\"project\",
\"sessions\")' in `agent/cli/ls.py').  Each session already carries
its `project' field via `AgentRow.__pm_json__', so flattening just
concatenates every group's `sessions' list."
  (apply #'append
         (mapcar (lambda (group) (alist-get 'sessions group))
                 groups)))

(defun pm-agent-list-refresh ()
  "Refetch sessions from `pm agent ls --json' and re-render."
  (interactive)
  (unless pm-agent-list--scope
    (user-error "Not in a pm-agent-list buffer"))
  (let* ((scope pm-agent-list--scope)
         (project (car scope))
         (all (cdr scope))
         (buf (current-buffer)))
    (pm--agent-ls
     project all nil
     (lambda (groups)
       (when (buffer-live-p buf)
         (with-current-buffer buf
           (setq pm-agent-list--data
                 (pm-agent-list--flatten-groups groups))
           (pm-agent-list--render)))))))

(defun pm-agent-list-act-at-point ()
  "Resume the session at point via `pm-agent-dispatch-function'."
  (interactive)
  (let* ((sec (magit-current-section))
         (target (pm-section-ancestor-of-type sec '(pm-session))))
    (unless target
      (user-error "No session at point"))
    (let ((row (oref target value)))
      (pm-agent--dispatch
       (list :agent      (alist-get 'agent row)
             :session-id (alist-get 'session_id row)
             :project    (alist-get 'project row))))))

;;;; Entry points

;;;###autoload
(defun pm-agent-launch (agent &optional project)
  "Launch a fresh AGENT session in pm PROJECT.

AGENT is one of \"claude\", \"codex\", \"cursor\" (the string keys
expected by `pm agent <name>').  PROJECT is the pm project
(container) name; nil prompts for one.

Routes through `pm-agent-dispatch-function' just like
`pm-agent-list-act-at-point' does for resumes — same dispatcher
choice (term / vterm / ghostel / custom) drives both fresh launches
and resumes."
  (interactive
   (list (completing-read "Agent: " '("claude" "codex" "cursor") nil t)
         (pm--read-project "In project: ")))
  (let ((proj (or project (pm--read-project "In project: "))))
    (pm-agent--dispatch
     (list :agent agent :session-id nil :project proj))))

;;;###autoload
(defun pm-agent-list (&optional project all)
  "Open a magit-style buffer listing recent agent sessions across pm projects.

With no arguments (or interactively with no prefix), shows sessions
from every project — i.e. `pm agent ls --all'.  Use
`pm-agent-list-current-project' for the single-project view.

PROJECT (a name) limits to one project; ALL forces the
all-projects view.  Interactively, a prefix arg prompts for a
project to scope to."
  (interactive
   (cond
    (current-prefix-arg (list (pm--read-project "Sessions for: ") nil))
    (t                  (list nil t))))
  (let ((buf (get-buffer-create (pm-agent-list--buffer-name project all))))
    (with-current-buffer buf
      (pm-agent-list-mode)
      (setq pm-agent-list--scope (cons project (and all t)))
      (setq default-directory
            (if project
                (pm-agent--cwd project)
              (pm-global-default-directory)))
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert "Loading…\n"))
      (pm-agent-list-refresh))
    (pop-to-buffer buf)
    (pm-section-set-window-margin)))

;;;###autoload
(defun pm-agent-list-current-project ()
  "Open `pm-agent-list' scoped to the current pm project.

\"Current\" is resolved in priority order:
  1. The transient scope (`(transient-scope)') — so this also
     works as a `pm-project-dispatch' suffix.  By the time a
     suffix runs, `default-directory' has reverted to the
     original buffer (potentially a different project, or none),
     so the bound project must come from the transient prefix.
  2. `pm-status--project' — set in `*pm-status: <name>*' buffers.
  3. The pm container that owns `default-directory'.
  4. A prompt, when none of the above identify a project."
  (interactive)
  (cl-flet ((container-name (path)
              (when-let ((c (and path (pm--container-of path))))
                (file-name-nondirectory (directory-file-name c)))))
    (let ((name
           (or (container-name (and (fboundp 'transient-scope)
                                    (transient-scope)))
               (and (bound-and-true-p pm-status--project)
                    pm-status--project)
               (container-name default-directory)
               (pm--read-project "Sessions for: "))))
      (pm-agent-list name nil))))

(provide 'pm-agent)

;;; pm-agent.el ends here
