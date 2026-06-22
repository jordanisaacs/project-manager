;;; pm-sidebar.el --- Live agent-session sidebar over SSE -*- lexical-binding: t -*-

;;; Commentary:

;; A magit-section sidebar that streams live agent-session state from the
;; `pm agent serve' daemon (GET /api/stream, server-side filtered to the
;; sessions this Emacs launched) and renders them grouped by project, with
;; status color-coded and the "waiting on you" states highlighted.
;;
;; The daemon forwards each session's `PM_META_*' env vars into its metadata.
;; `pm-agent.el' seeds two on launch: `PM_META_SOURCE' (a tag we filter on)
;; and `PM_META_BUF' (the launching buffer's builtin id), so RET on a row can
;; jump straight to the agent's terminal buffer.
;;
;; Entry point: `pm-sidebar' (show), `pm-sidebar-quit' (hide).

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'json)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)   ; pm-propertize-face + section reset/cover/show helpers
(require 'pm-agent)   ; pm-agent-serve-source, pm-agent-buffer-for-id, pm-agent--cwd

;; Autoloaded from pm-transient; called by RET on a project heading.
(declare-function pm-project-dispatch "pm-transient" (&optional arg))

(defgroup pm-sidebar nil
  "Live agent-session sidebar fed by `pm agent serve'."
  :group 'pm)

(defcustom pm-sidebar-port 8787
  "Loopback TCP port the `pm agent serve' daemon listens on."
  :type 'integer
  :group 'pm-sidebar)

(defcustom pm-sidebar-width 29
  "Default total width of the sidebar side window, in columns."
  :type 'integer
  :group 'pm-sidebar)

(defconst pm-sidebar-buffer-name "*pm-agents*")

;; Heading shown for sessions with no resolved pm project. Kept as a constant
;; so the render and the RET handler agree on what is *not* a real project.
(defconst pm-sidebar--no-project "(no project)")

;; --- faces ------------------------------------------------------------------
(defface pm-sidebar-working    '((t :inherit success :weight bold)) "Agent is working.")
(defface pm-sidebar-response   '((t :inherit warning :weight bold)) "Awaiting your reply.")
(defface pm-sidebar-permission '((t :inherit error :weight bold))   "Needs your approval.")
(defface pm-sidebar-idle       '((t :inherit shadow))               "Idle.")
(defface pm-sidebar-age        '((t :inherit shadow))               "Last-active age.")
(defface pm-sidebar-detail     '((t :inherit shadow :slant italic)) "Tool/activity detail line.")
(defface pm-sidebar-rule       '((t :inherit shadow))               "Separator rule.")

;; status string -> (glyph face row-emphasised?).
(defconst pm-sidebar--status
  '(("working"            "●" pm-sidebar-working    nil)
    ("waiting_response"   "◆" pm-sidebar-response   t)
    ("waiting_permission" "▲" pm-sidebar-permission t)
    ("blocked"            "◆" pm-sidebar-response   t)  ; pre-split daemons
    ("idle"               "○" pm-sidebar-idle       nil)
    ("unknown"            "◌" pm-sidebar-idle       nil)))

(defconst pm-sidebar--age-width 5)

;; --- runtime state ----------------------------------------------------------
(defvar pm-sidebar--proc nil)
(defvar pm-sidebar--acc "")
(defvar pm-sidebar--headers-done nil)
(defvar pm-sidebar--sessions (make-hash-table :test 'equal))
(defvar pm-sidebar--last-width nil)
(defvar pm-sidebar--want-connection nil
  "Non-nil while the sidebar should stay connected; cleared by `pm-sidebar-quit'.")
(defvar pm-sidebar--reconnect-timer nil
  "Pending reconnect timer, so a dropped stream (e.g. daemon restart) recovers.")

;; --- text helpers -----------------------------------------------------------
(defun pm-sidebar--ellipsize (str width)
  "Truncate STR to WIDTH with a trailing `...' (no padding)."
  (if (> (length str) width)
      (concat (substring str 0 (max 0 (- width 3))) "...")
    str))

(defun pm-sidebar--fit (str width)
  "Ellipsize STR to WIDTH, then right-pad with spaces to exactly WIDTH."
  (let ((cut (pm-sidebar--ellipsize str width)))
    (concat cut (make-string (max 0 (- width (length cut))) ?\s))))

(defun pm-sidebar--rjust (str width)
  (let ((len (length str)))
    (if (>= len width) str (concat (make-string (- width len) ?\s) str))))

(defun pm-sidebar--age (ms)
  (if (or (null ms) (<= ms 0)) "·"
    (let ((s (- (float-time) (/ ms 1000.0))))
      (cond ((< s 60) "now")
            ((< s 3600) (format "%dm" (floor (/ s 60))))
            ((< s 86400) (format "%dh" (floor (/ s 3600))))
            (t (format "%dd" (floor (/ s 86400))))))))

(defun pm-sidebar--width ()
  "Usable text width of the sidebar window (accounts for fringes/margin)."
  (let ((win (get-buffer-window pm-sidebar-buffer-name t)))
    (if win (max 24 (window-max-chars-per-line win)) pm-sidebar-width)))

(defun pm-sidebar--rule (width)
  (pm-propertize-face (concat (make-string (max 0 width) ?─) "\n") 'pm-sidebar-rule))

(defun pm-sidebar--detail (s status)
  "Second-line tool/activity detail for session S, or nil to omit."
  (let ((tool (alist-get 'current_tool s))
        (activity (alist-get 'activity s)))
    (cond
     ((equal status "waiting_permission") (format "approve: %s" (or tool "tool")))
     ((equal status "waiting_response") "awaiting your reply")
     ((and tool activity (not (equal activity tool))) (format "%s · %s" activity tool))
     (tool tool)
     ((equal status "working") (or activity "working"))
     (t nil))))

;; --- rendering --------------------------------------------------------------
(defun pm-sidebar--session-key (s)
  (concat (or (alist-get 'agent s) "") "\0" (or (alist-get 'vendor_session_id s) "")))

(defun pm-sidebar--insert-session (s width)
  (let* ((status (or (alist-get 'status s) "unknown"))
         (spec (assoc status pm-sidebar--status))
         (glyph (or (nth 1 spec) "◌"))
         (face (or (nth 2 spec) 'pm-sidebar-idle))
         (emph (nth 3 spec))
         (title (or (alist-get 'title s) "(untitled)"))
         (age (pm-sidebar--age (alist-get 'last_activity_at s)))
         (text-face (if emph face 'default))
         (room (max 6 (- width 2 pm-sidebar--age-width 1)))
         (detail (pm-sidebar--detail s status)))
    ;; Each session is its own subsection: line 1 (status, title, age) is the
    ;; heading; the tool/activity detail is the body, so folding hides it.
    (magit-insert-section (pm-sidebar-session s)
      (magit-insert-heading
        (concat (pm-propertize-face glyph face) " "
                (pm-propertize-face (pm-sidebar--fit title room) text-face)
                " " (pm-propertize-face (pm-sidebar--rjust age pm-sidebar--age-width) 'pm-sidebar-age)))
      (when detail
        (insert "  "
                (pm-propertize-face (concat "↳ " (pm-sidebar--ellipsize detail (- width 4)))
                                    'pm-sidebar-detail)
                "\n")))))

(defun pm-sidebar--insert-legend ()
  (insert (pm-propertize-face "● " 'pm-sidebar-working) (pm-propertize-face "working   " 'pm-sidebar-idle)
          (pm-propertize-face "○ " 'pm-sidebar-idle) (pm-propertize-face "idle" 'pm-sidebar-idle)
          "\n"
          (pm-propertize-face "◆ " 'pm-sidebar-response) (pm-propertize-face "reply     " 'pm-sidebar-idle)
          (pm-propertize-face "▲ " 'pm-sidebar-permission) (pm-propertize-face "approve" 'pm-sidebar-idle)
          "\n"))

(defun pm-sidebar--render ()
  (let* ((buf (get-buffer-create pm-sidebar-buffer-name))
         (win (get-buffer-window buf t)))
    (with-current-buffer buf
      (unless (derived-mode-p 'pm-sidebar-mode) (pm-sidebar-mode))
      (let ((width (pm-sidebar--width))
            (inhibit-read-only t)
            ;; Capture point + scroll BEFORE the erase so a refresh (which
            ;; rebuilds the whole buffer) doesn't yank the cursor to the top.
            (pt (if (window-live-p win) (window-point win) (point)))
            (ws (and (window-live-p win) (window-start win))))
        (setq pm-sidebar--last-width width)
        (pm-table-reset-section-state)
        (erase-buffer)
        (magit-insert-section (pm-sidebar-root)
          (insert (pm-table-banner "pm agents" (hash-table-count pm-sidebar--sessions)))
          (insert "\n" (pm-sidebar--rule width))
          (if (zerop (hash-table-count pm-sidebar--sessions))
              (insert "\n" (pm-propertize-face "no active sessions" 'pm-sidebar-idle) "\n")
            (let ((groups (make-hash-table :test 'equal)) (order '()))
              (maphash (lambda (_k s)
                         (let ((p (or (alist-get 'project s) pm-sidebar--no-project)))
                           (unless (gethash p groups) (push p order))
                           (push s (gethash p groups))))
                       pm-sidebar--sessions)
              (dolist (p (nreverse order))
                (insert "\n")
                (magit-insert-section (pm-sidebar-project p)
                  (magit-insert-heading (pm-propertize-face p 'pm-group-heading))
                  (dolist (s (sort (gethash p groups)
                                   (lambda (a b) (> (or (alist-get 'last_activity_at a) 0)
                                                    (or (alist-get 'last_activity_at b) 0)))))
                    (pm-sidebar--insert-session s width))))))
          (insert "\n" (pm-sidebar--rule width))
          (pm-sidebar--insert-legend))
        (pm-table-cover-root-section)
        (pm-table-show-root-section)
        ;; Restore point + scroll (clamped — the buffer may have shrunk).
        (goto-char (min pt (point-max)))
        (when (window-live-p win)
          (set-window-point win (point))
          (when ws (set-window-start win (min ws (point-max)) t)))))))

;; --- actions ----------------------------------------------------------------
(defun pm-sidebar-visit ()
  "Act on the thing at point.
On a session row, jump to its terminal buffer (via the session's
`PM_META_BUF').  On a project heading, open `pm-project-dispatch' scoped to
that project."
  (interactive)
  (let* ((sec (magit-current-section))
         (type (and sec (oref sec type)))
         (val (and sec (oref sec value))))
    (cond
     ((eq type 'pm-sidebar-session)
      (let ((buf (pm-agent-buffer-for-id (alist-get 'BUF (alist-get 'meta val)))))
        (if buf (pop-to-buffer buf)
          (user-error "No live buffer for this session (not launched here, or closed)"))))
     ((eq type 'pm-sidebar-project)
      (when (equal val pm-sidebar--no-project)
        (user-error "Not a pm project"))
      ;; Scope the dispatch to this project's container so
      ;; `pm--resolve-dispatch-path' (which keys off `default-directory') picks
      ;; it without prompting.
      (let ((default-directory (pm-agent--cwd val)))
        (pm-project-dispatch)))
     (t (user-error "Point is not on a session or project")))))

;; --- SSE plumbing -----------------------------------------------------------
(defun pm-sidebar--filter-query ()
  ;; Scope to sessions THIS Emacs launched: SOURCE marks "from Emacs",
  ;; EMACS pins the specific instance (the daemon is shared, so filtering on
  ;; SOURCE alone would surface other Emacs instances' agents). The daemon
  ;; ANDs the params.
  (format "meta.SOURCE=%s&meta.EMACS=%s"
          pm-agent-serve-source (pm-agent--emacs-id)))

(defun pm-sidebar--handle (json)
  (when (> (length json) 0)
    (condition-case nil
        (let* ((obj (json-parse-string json :object-type 'alist :array-type 'list
                                       :null-object nil :false-object nil))
               (type (alist-get 'type obj)))
          (cond
           ((equal type "snapshot")
            (clrhash pm-sidebar--sessions)
            (dolist (s (alist-get 'sessions obj))
              (puthash (pm-sidebar--session-key s) s pm-sidebar--sessions)))
           ((equal type "update")
            (let ((s (alist-get 'session obj)))
              (puthash (pm-sidebar--session-key s) s pm-sidebar--sessions)))
           ((equal type "remove")
            ;; `agent'/`vendor_session_id' sit at the top level of the
            ;; remove payload, exactly the keys `session-key' reads.
            (remhash (pm-sidebar--session-key obj) pm-sidebar--sessions)))
          (pm-sidebar--render))
      (error nil))))

(defun pm-sidebar--proc-filter (_proc chunk)
  ;; Normalize CRLF→LF and key off \n; skip HTTP headers once.
  (setq pm-sidebar--acc (concat pm-sidebar--acc (replace-regexp-in-string "\r" "" chunk)))
  (unless pm-sidebar--headers-done
    (let ((idx (string-search "\n\n" pm-sidebar--acc)))
      (when idx
        (setq pm-sidebar--acc (substring pm-sidebar--acc (+ idx 2))
              pm-sidebar--headers-done t))))
  (when pm-sidebar--headers-done
    (let (nl)
      (while (setq nl (string-search "\n" pm-sidebar--acc))
        (let ((line (string-trim (substring pm-sidebar--acc 0 nl))))
          (setq pm-sidebar--acc (substring pm-sidebar--acc (1+ nl)))
          (when (string-prefix-p "data:" line)
            (pm-sidebar--handle (string-trim (substring line 5)))))))))

(defun pm-sidebar--schedule-reconnect ()
  "Arm a one-shot reconnect, unless we deliberately disconnected.
Lets the sidebar recover after the daemon restarts or briefly goes away."
  (when (and pm-sidebar--want-connection (not (timerp pm-sidebar--reconnect-timer)))
    (setq pm-sidebar--reconnect-timer
          (run-at-time 3 nil
                       (lambda ()
                         (setq pm-sidebar--reconnect-timer nil)
                         (when (and pm-sidebar--want-connection
                                    (get-buffer pm-sidebar-buffer-name))
                           (pm-sidebar--connect)))))))

(defun pm-sidebar--send-request (proc)
  "Send the SSE GET request once PROC's connection is open."
  (process-send-string
   proc
   (format (concat "GET /api/stream?%s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   "Accept: text/event-stream\r\nConnection: keep-alive\r\n\r\n")
           (pm-sidebar--filter-query))))

(defun pm-sidebar--sentinel (proc event)
  "Drive the async SSE connection: send the request on open, reconnect on drop.
PROC is created with `:nowait', so the connect completes here rather than
blocking Emacs's main loop (a blocking connect to a restarting daemon would
otherwise wedge the whole daemon)."
  (cond
   ((string-prefix-p "open" event)
    (pm-sidebar--send-request proc))
   ((and pm-sidebar--want-connection (not (process-live-p proc)))
    ;; Connection failed or dropped (daemon restart / down) — retry later.
    (pm-sidebar--schedule-reconnect))))

(defun pm-sidebar--connect ()
  ;; Detach the old proc's sentinel first so tearing it down here doesn't look
  ;; like an unexpected drop and trigger a spurious reconnect.
  (when (process-live-p pm-sidebar--proc)
    (set-process-sentinel pm-sidebar--proc #'ignore)
    (delete-process pm-sidebar--proc))
  (setq pm-sidebar--acc "" pm-sidebar--headers-done nil pm-sidebar--want-connection t)
  (clrhash pm-sidebar--sessions)
  ;; `:nowait t' makes the connect asynchronous — make-network-process returns
  ;; immediately and the sentinel fires on open/failure. A blocking connect
  ;; (the default) stalls the entire Emacs main loop until it resolves, which
  ;; wedges the daemon when reconnecting to a mid-restart `pm agent serve'.
  (condition-case nil
      (setq pm-sidebar--proc
            (make-network-process
             :name "pm-sidebar-sse" :host "127.0.0.1" :service pm-sidebar-port
             ;; `:noquery t' keeps this background stream out of the "Active
             ;; processes exist; kill them?" exit prompt, so Emacs closes
             ;; cleanly without asking about the sidebar connection.
             :coding 'utf-8 :nowait t :noquery t :filter #'pm-sidebar--proc-filter
             :sentinel #'pm-sidebar--sentinel))
    (error (setq pm-sidebar--proc nil)
           (pm-sidebar--schedule-reconnect))))

(defun pm-sidebar--on-window-size-change (_frame)
  "Re-fit the sidebar when its window's usable width changes."
  (let ((win (get-buffer-window pm-sidebar-buffer-name t)))
    (when (and win (/= (max 24 (window-max-chars-per-line win)) (or pm-sidebar--last-width -1)))
      (pm-sidebar--render))))

;; --- mode + entry points ----------------------------------------------------
(defvar pm-sidebar-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)  ; TAB fold, n/p nav, etc.
    (define-key map (kbd "RET") #'pm-sidebar-visit)
    (define-key map (kbd "g")   #'pm-sidebar-refresh)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `pm-sidebar-mode'.")
;; Force the parent on reload — `defvar' won't update an existing keymap.
(set-keymap-parent pm-sidebar-mode-map magit-section-mode-map)

(define-derived-mode pm-sidebar-mode magit-section-mode "PM-Agents"
  "Live agent-session sidebar."
  (setq-local mode-line-format nil
              cursor-type nil
              truncate-lines t
              display-line-numbers nil)
  ;; Allocate the left margin for the section visibility indicator (uses the
  ;; config's `magit-section-visibility-indicators' as-is).
  (pm-section-setup-margin))

(defun pm-sidebar-refresh ()
  "Reconnect to the daemon and redraw."
  (interactive)
  (pm-sidebar--connect)
  (pm-sidebar--render))

(defun pm-sidebar--open ()
  "Open (or reveal) the sidebar in a left side window, connecting if needed.
Reuses the existing SSE connection when one is live, so toggling closed and
back open is instant and keeps the current session state."
  (unless (process-live-p pm-sidebar--proc)
    (pm-sidebar--connect))
  (get-buffer-create pm-sidebar-buffer-name)
  (display-buffer
   (get-buffer pm-sidebar-buffer-name)
   `((display-buffer-in-side-window) (side . left) (slot . 0)
     (window-width . ,pm-sidebar-width)
     (window-parameters . ((no-delete-other-windows . t)))))
  (pm-sidebar--render)  ; after display so it fits the real window width
  (add-hook 'window-size-change-functions #'pm-sidebar--on-window-size-change)
  pm-sidebar-buffer-name)

;;;###autoload
(defun pm-sidebar ()
  "Toggle the live agent-session sidebar (streams from `pm agent serve').
Open it if hidden; hide its window if already showing (the connection stays
live for an instant re-open).  `pm-sidebar-quit' tears the connection down."
  (interactive)
  (if-let ((win (get-buffer-window pm-sidebar-buffer-name t)))
      (delete-window win)
    (pm-sidebar--open)))

(defun pm-sidebar-quit ()
  "Disconnect and remove the sidebar."
  (interactive)
  ;; Stop auto-reconnect before tearing the stream down, else the sentinel
  ;; would immediately re-dial the daemon.
  (setq pm-sidebar--want-connection nil)
  (when (timerp pm-sidebar--reconnect-timer)
    (cancel-timer pm-sidebar--reconnect-timer)
    (setq pm-sidebar--reconnect-timer nil))
  (remove-hook 'window-size-change-functions #'pm-sidebar--on-window-size-change)
  (when (process-live-p pm-sidebar--proc)
    (set-process-sentinel pm-sidebar--proc #'ignore)
    (delete-process pm-sidebar--proc))
  (when-let ((b (get-buffer pm-sidebar-buffer-name)))
    (when-let ((w (get-buffer-window b t))) (delete-window w))
    (kill-buffer b)))

(provide 'pm-sidebar)
;;; pm-sidebar.el ends here
