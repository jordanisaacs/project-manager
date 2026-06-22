;;; pm-serve.el --- Live agent-session sidebar over SSE -*- lexical-binding: t -*-

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
;; Entry point: `pm-serve' (show), `pm-serve-quit' (hide).

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'json)
(require 'magit-section)

(require 'pm-faces)
(require 'pm-table)   ; pm-propertize-face + section reset/cover/show helpers
(require 'pm-agent)   ; pm-agent-serve-source, pm-agent-buffer-for-id

(defgroup pm-serve nil
  "Live agent-session sidebar fed by `pm agent serve'."
  :group 'pm)

(defcustom pm-serve-port 8787
  "Loopback TCP port the `pm agent serve' daemon listens on."
  :type 'integer
  :group 'pm-serve)

(defcustom pm-serve-width 29
  "Default total width of the sidebar side window, in columns."
  :type 'integer
  :group 'pm-serve)

(defconst pm-serve-buffer-name "*pm-agents*")

;; --- faces ------------------------------------------------------------------
(defface pm-serve-working    '((t :inherit success :weight bold)) "Agent is working.")
(defface pm-serve-response   '((t :inherit warning :weight bold)) "Awaiting your reply.")
(defface pm-serve-permission '((t :inherit error :weight bold))   "Needs your approval.")
(defface pm-serve-idle       '((t :inherit shadow))               "Idle.")
(defface pm-serve-age        '((t :inherit shadow))               "Last-active age.")
(defface pm-serve-detail     '((t :inherit shadow :slant italic)) "Tool/activity detail line.")
(defface pm-serve-rule       '((t :inherit shadow))               "Separator rule.")

;; status string -> (glyph face row-emphasised?).
(defconst pm-serve--status
  '(("working"            "●" pm-serve-working    nil)
    ("waiting_response"   "◆" pm-serve-response   t)
    ("waiting_permission" "▲" pm-serve-permission t)
    ("blocked"            "◆" pm-serve-response   t)  ; pre-split daemons
    ("idle"               "○" pm-serve-idle       nil)
    ("unknown"            "◌" pm-serve-idle       nil)))

(defconst pm-serve--age-width 5)

;; --- runtime state ----------------------------------------------------------
(defvar pm-serve--proc nil)
(defvar pm-serve--acc "")
(defvar pm-serve--headers-done nil)
(defvar pm-serve--sessions (make-hash-table :test 'equal))
(defvar pm-serve--last-width nil)
(defvar pm-serve--want-connection nil
  "Non-nil while the sidebar should stay connected; cleared by `pm-serve-quit'.")
(defvar pm-serve--reconnect-timer nil
  "Pending reconnect timer, so a dropped stream (e.g. daemon restart) recovers.")

;; --- text helpers -----------------------------------------------------------
(defun pm-serve--ellipsize (str width)
  "Truncate STR to WIDTH with a trailing `...' (no padding)."
  (if (> (length str) width)
      (concat (substring str 0 (max 0 (- width 3))) "...")
    str))

(defun pm-serve--fit (str width)
  "Ellipsize STR to WIDTH, then right-pad with spaces to exactly WIDTH."
  (let ((cut (pm-serve--ellipsize str width)))
    (concat cut (make-string (max 0 (- width (length cut))) ?\s))))

(defun pm-serve--rjust (str width)
  (let ((len (length str)))
    (if (>= len width) str (concat (make-string (- width len) ?\s) str))))

(defun pm-serve--age (ms)
  (if (or (null ms) (<= ms 0)) "·"
    (let ((s (- (float-time) (/ ms 1000.0))))
      (cond ((< s 60) "now")
            ((< s 3600) (format "%dm" (floor (/ s 60))))
            ((< s 86400) (format "%dh" (floor (/ s 3600))))
            (t (format "%dd" (floor (/ s 86400))))))))

(defun pm-serve--width ()
  "Usable text width of the sidebar window (accounts for fringes/margin)."
  (let ((win (get-buffer-window pm-serve-buffer-name t)))
    (if win (max 24 (window-max-chars-per-line win)) pm-serve-width)))

(defun pm-serve--rule (width)
  (pm-propertize-face (concat (make-string (max 0 width) ?─) "\n") 'pm-serve-rule))

(defun pm-serve--detail (s status)
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
(defun pm-serve--session-key (s)
  (concat (or (alist-get 'agent s) "") "\0" (or (alist-get 'vendor_session_id s) "")))

(defun pm-serve--insert-session (s width)
  (let* ((status (or (alist-get 'status s) "unknown"))
         (spec (assoc status pm-serve--status))
         (glyph (or (nth 1 spec) "◌"))
         (face (or (nth 2 spec) 'pm-serve-idle))
         (emph (nth 3 spec))
         (title (or (alist-get 'title s) "(untitled)"))
         (age (pm-serve--age (alist-get 'last_activity_at s)))
         (text-face (if emph face 'default))
         (room (max 6 (- width 2 pm-serve--age-width 1)))
         (detail (pm-serve--detail s status)))
    ;; Each session is its own subsection: line 1 (status, title, age) is the
    ;; heading; the tool/activity detail is the body, so folding hides it.
    (magit-insert-section (pm-serve-session s)
      (magit-insert-heading
        (concat (pm-propertize-face glyph face) " "
                (pm-propertize-face (pm-serve--fit title room) text-face)
                " " (pm-propertize-face (pm-serve--rjust age pm-serve--age-width) 'pm-serve-age)))
      (when detail
        (insert "  "
                (pm-propertize-face (concat "↳ " (pm-serve--ellipsize detail (- width 4)))
                                    'pm-serve-detail)
                "\n")))))

(defun pm-serve--insert-legend ()
  (insert (pm-propertize-face "● " 'pm-serve-working) (pm-propertize-face "working   " 'pm-serve-idle)
          (pm-propertize-face "○ " 'pm-serve-idle) (pm-propertize-face "idle" 'pm-serve-idle)
          "\n"
          (pm-propertize-face "◆ " 'pm-serve-response) (pm-propertize-face "reply     " 'pm-serve-idle)
          (pm-propertize-face "▲ " 'pm-serve-permission) (pm-propertize-face "approve" 'pm-serve-idle)
          "\n"))

(defun pm-serve--render ()
  (let ((buf (get-buffer-create pm-serve-buffer-name)))
    (with-current-buffer buf
      (unless (derived-mode-p 'pm-serve-mode) (pm-serve-mode))
      (let ((width (pm-serve--width))
            (inhibit-read-only t))
        (setq pm-serve--last-width width)
        (pm-table-reset-section-state)
        (erase-buffer)
        (magit-insert-section (pm-serve-root)
          (insert (pm-table-banner "pm agents" (hash-table-count pm-serve--sessions)))
          (insert "\n" (pm-serve--rule width))
          (if (zerop (hash-table-count pm-serve--sessions))
              (insert "\n" (pm-propertize-face "no active sessions" 'pm-serve-idle) "\n")
            (let ((groups (make-hash-table :test 'equal)) (order '()))
              (maphash (lambda (_k s)
                         (let ((p (or (alist-get 'project s) "(no project)")))
                           (unless (gethash p groups) (push p order))
                           (push s (gethash p groups))))
                       pm-serve--sessions)
              (dolist (p (nreverse order))
                (insert "\n")
                (magit-insert-section (pm-serve-project p)
                  (magit-insert-heading (pm-propertize-face p 'pm-group-heading))
                  (dolist (s (sort (gethash p groups)
                                   (lambda (a b) (> (or (alist-get 'last_activity_at a) 0)
                                                    (or (alist-get 'last_activity_at b) 0)))))
                    (pm-serve--insert-session s width))))))
          (insert "\n" (pm-serve--rule width))
          (pm-serve--insert-legend))
        (pm-table-cover-root-section)
        (pm-table-show-root-section)
        (goto-char (point-min))))))

;; --- actions ----------------------------------------------------------------
(defun pm-serve-visit ()
  "Jump to the terminal buffer of the agent session at point.
Uses the session's `PM_META_BUF' (the launching buffer's builtin id)."
  (interactive)
  (let* ((sec (magit-current-section))
         (s (and sec (eq (oref sec type) 'pm-serve-session) (oref sec value)))
         (bufid (and s (alist-get 'BUF (alist-get 'meta s))))
         (buf (pm-agent-buffer-for-id bufid)))
    (cond (buf (pop-to-buffer buf))
          (s (user-error "No live buffer for this session (not launched here, or closed)"))
          (t (user-error "Point is not on a session")))))

;; --- SSE plumbing -----------------------------------------------------------
(defun pm-serve--filter-query ()
  (format "meta.SOURCE=%s" pm-agent-serve-source))

(defun pm-serve--handle (json)
  (when (> (length json) 0)
    (condition-case nil
        (let* ((obj (json-parse-string json :object-type 'alist :array-type 'list
                                       :null-object nil :false-object nil))
               (type (alist-get 'type obj)))
          (cond
           ((equal type "snapshot")
            (clrhash pm-serve--sessions)
            (dolist (s (alist-get 'sessions obj))
              (puthash (pm-serve--session-key s) s pm-serve--sessions)))
           ((equal type "update")
            (let ((s (alist-get 'session obj)))
              (puthash (pm-serve--session-key s) s pm-serve--sessions)))
           ((equal type "remove")
            ;; `agent'/`vendor_session_id' sit at the top level of the
            ;; remove payload, exactly the keys `session-key' reads.
            (remhash (pm-serve--session-key obj) pm-serve--sessions)))
          (pm-serve--render))
      (error nil))))

(defun pm-serve--proc-filter (_proc chunk)
  ;; Normalize CRLF→LF and key off \n; skip HTTP headers once.
  (setq pm-serve--acc (concat pm-serve--acc (replace-regexp-in-string "\r" "" chunk)))
  (unless pm-serve--headers-done
    (let ((idx (string-search "\n\n" pm-serve--acc)))
      (when idx
        (setq pm-serve--acc (substring pm-serve--acc (+ idx 2))
              pm-serve--headers-done t))))
  (when pm-serve--headers-done
    (let (nl)
      (while (setq nl (string-search "\n" pm-serve--acc))
        (let ((line (string-trim (substring pm-serve--acc 0 nl))))
          (setq pm-serve--acc (substring pm-serve--acc (1+ nl)))
          (when (string-prefix-p "data:" line)
            (pm-serve--handle (string-trim (substring line 5)))))))))

(defun pm-serve--schedule-reconnect ()
  "Arm a one-shot reconnect, unless we deliberately disconnected.
Lets the sidebar recover after the daemon restarts or briefly goes away."
  (when (and pm-serve--want-connection (not (timerp pm-serve--reconnect-timer)))
    (setq pm-serve--reconnect-timer
          (run-at-time 3 nil
                       (lambda ()
                         (setq pm-serve--reconnect-timer nil)
                         (when (and pm-serve--want-connection
                                    (get-buffer pm-serve-buffer-name))
                           (pm-serve--connect)))))))

(defun pm-serve--send-request (proc)
  "Send the SSE GET request once PROC's connection is open."
  (process-send-string
   proc
   (format (concat "GET /api/stream?%s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   "Accept: text/event-stream\r\nConnection: keep-alive\r\n\r\n")
           (pm-serve--filter-query))))

(defun pm-serve--sentinel (proc event)
  "Drive the async SSE connection: send the request on open, reconnect on drop.
PROC is created with `:nowait', so the connect completes here rather than
blocking Emacs's main loop (a blocking connect to a restarting daemon would
otherwise wedge the whole daemon)."
  (cond
   ((string-prefix-p "open" event)
    (pm-serve--send-request proc))
   ((and pm-serve--want-connection (not (process-live-p proc)))
    ;; Connection failed or dropped (daemon restart / down) — retry later.
    (pm-serve--schedule-reconnect))))

(defun pm-serve--connect ()
  ;; Detach the old proc's sentinel first so tearing it down here doesn't look
  ;; like an unexpected drop and trigger a spurious reconnect.
  (when (process-live-p pm-serve--proc)
    (set-process-sentinel pm-serve--proc #'ignore)
    (delete-process pm-serve--proc))
  (setq pm-serve--acc "" pm-serve--headers-done nil pm-serve--want-connection t)
  (clrhash pm-serve--sessions)
  ;; `:nowait t' makes the connect asynchronous — make-network-process returns
  ;; immediately and the sentinel fires on open/failure. A blocking connect
  ;; (the default) stalls the entire Emacs main loop until it resolves, which
  ;; wedges the daemon when reconnecting to a mid-restart `pm agent serve'.
  (condition-case nil
      (setq pm-serve--proc
            (make-network-process
             :name "pm-serve-sse" :host "127.0.0.1" :service pm-serve-port
             :coding 'utf-8 :nowait t :filter #'pm-serve--proc-filter
             :sentinel #'pm-serve--sentinel))
    (error (setq pm-serve--proc nil)
           (pm-serve--schedule-reconnect))))

(defun pm-serve--on-window-size-change (_frame)
  "Re-fit the sidebar when its window's usable width changes."
  (let ((win (get-buffer-window pm-serve-buffer-name t)))
    (when (and win (/= (max 24 (window-max-chars-per-line win)) (or pm-serve--last-width -1)))
      (pm-serve--render))))

;; --- mode + entry points ----------------------------------------------------
(defvar pm-serve-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map magit-section-mode-map)  ; TAB fold, n/p nav, etc.
    (define-key map (kbd "RET") #'pm-serve-visit)
    (define-key map (kbd "g")   #'pm-serve-refresh)
    (define-key map (kbd "q")   #'quit-window)
    map)
  "Keymap for `pm-serve-mode'.")
;; Force the parent on reload — `defvar' won't update an existing keymap.
(set-keymap-parent pm-serve-mode-map magit-section-mode-map)

(define-derived-mode pm-serve-mode magit-section-mode "PM-Agents"
  "Live agent-session sidebar."
  (setq-local mode-line-format nil
              cursor-type nil
              truncate-lines t
              display-line-numbers nil)
  ;; Allocate the left margin for the section visibility indicator (uses the
  ;; config's `magit-section-visibility-indicators' as-is).
  (pm-section-setup-margin))

(defun pm-serve-refresh ()
  "Reconnect to the daemon and redraw."
  (interactive)
  (pm-serve--connect)
  (pm-serve--render))

;;;###autoload
(defun pm-serve ()
  "Show the live agent-session sidebar, streaming from `pm agent serve'."
  (interactive)
  (pm-serve--connect)
  (get-buffer-create pm-serve-buffer-name)
  (display-buffer
   (get-buffer pm-serve-buffer-name)
   `((display-buffer-in-side-window) (side . left) (slot . 0)
     (window-width . ,pm-serve-width)
     (window-parameters . ((no-delete-other-windows . t)))))
  (pm-serve--render)  ; after display so it fits the real window width
  (add-hook 'window-size-change-functions #'pm-serve--on-window-size-change)
  pm-serve-buffer-name)

(defun pm-serve-quit ()
  "Disconnect and remove the sidebar."
  (interactive)
  ;; Stop auto-reconnect before tearing the stream down, else the sentinel
  ;; would immediately re-dial the daemon.
  (setq pm-serve--want-connection nil)
  (when (timerp pm-serve--reconnect-timer)
    (cancel-timer pm-serve--reconnect-timer)
    (setq pm-serve--reconnect-timer nil))
  (remove-hook 'window-size-change-functions #'pm-serve--on-window-size-change)
  (when (process-live-p pm-serve--proc)
    (set-process-sentinel pm-serve--proc #'ignore)
    (delete-process pm-serve--proc))
  (when-let ((b (get-buffer pm-serve-buffer-name)))
    (when-let ((w (get-buffer-window b t))) (delete-window w))
    (kill-buffer b)))

(provide 'pm-serve)
;;; pm-serve.el ends here
