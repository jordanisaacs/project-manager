;;; pm-table.el --- Shared table + section utilities for pm buffers -*- lexical-binding: t -*-

;;; Commentary:

;; Small helpers shared by `pm-status', `pm-list', `pm-pool', `pm-repo',
;; `pm-agent'.  Kept out of `pm-faces.el' (which is purely defaces) and
;; out of `pm-status.el' / `pm-agent.el' so the latter two can depend on
;; each other through autoloads without a require cycle.
;;
;; Three concerns live here:
;;
;;   - column padding: `pm-table-row' / `pm-table-widths'
;;   - grouping / value-walking: `pm-table-group-by',
;;     `pm-section-ancestor-of-type'
;;   - timestamp formatting + Rich-markup parsing for stacker output

;;; Code:

(require 'cl-lib)
(require 'subr-x)
(require 'eieio)  ; for `oref' used by `pm-section-ancestor-of-type'
;; Compile-time only: lets the byte-compiler resolve the magit-section
;; class slots referenced by `oref' below without forcing a load-order
;; dependency on this file.
(eval-when-compile
  (require 'magit-section nil t))

(require 'pm-faces)

;;;; Column padding

(defun pm-table-widths (rows)
  "Compute column widths for ROWS (each a list of cell strings)."
  (let* ((n (when rows (length (car rows))))
         (widths (when n (make-vector n 0))))
    (dolist (r rows)
      (cl-loop for cell in r
               for i from 0
               do (aset widths i
                        (max (aref widths i) (length cell)))))
    (when widths (append widths nil))))

(defun pm-table-row (cells widths)
  "Format CELLS (list of strings) padded to WIDTHS.

The trailing cell isn't padded so the line doesn't carry useless
whitespace into the kill ring.  Two-space column separator (matches
the CLI's `padding=(0, 2)')."
  (let ((last (1- (length cells)))
        out)
    (cl-loop for cell in cells
             for w in widths
             for i from 0
             do (push (if (= i last) cell (format (format "%%-%ds" w) cell)) out))
    (string-join (nreverse out) "  ")))

;;;; Grouping

(defun pm-table-group-by (rows key)
  "Return ((KEY-VAL . ROWS-WITH-THAT-KEY) ...), preserving insertion order.

Each row in ROWS is an alist; KEY is a symbol looked up via
`alist-get'.  Stable order keeps the rendered table deterministic
across refreshes."
  (let (out)
    (dolist (r rows)
      (let* ((k (alist-get key r))
             (existing (assoc k out)))
        (if existing
            (setcdr existing (append (cdr existing) (list r)))
          (setq out (append out (list (cons k (list r))))))))
    out))

;;;; Left-margin allocation for visibility indicators

;; `magit-section-visibility-indicators' in (CHAR . CHAR) form renders
;; the indicator into the left margin, but `magit-section-mode' itself
;; doesn't allocate one — full magit does it via `magit-set-buffer-margins'
;; in `magit-setup-buffer-hook' (see magit-margin.el). pm only depends on
;; magit-section, so it mirrors `magit-set-window-margins' here.
;;
;; Each pm-*-mode calls `pm-section-setup-margin' from its mode body.
;; Each entry point (`pm-project-status', etc.) also calls
;; `pm-section-set-window-margin' after `pop-to-buffer' so the window's
;; margin is in place before redisplay paints the section indicators.

(declare-function magit-section-visibility-indicator "magit-section" ())

(defun pm-section-set-window-margin (&optional window)
  "Set WINDOW's left margin to match the active section visibility indicator.
Mirrors `magit-set-window-margins' (magit-margin.el): width 1 when the
indicator is a character (the form that renders in the left margin),
otherwise the existing left margin is preserved."
  (when (or window (setq window (get-buffer-window)))
    (with-selected-window window
      (set-window-margins
       nil
       (if (and (fboundp 'magit-section-visibility-indicator)
                (characterp (car (magit-section-visibility-indicator))))
           1
         (car (window-margins)))
       (cdr (window-margins))))))

(defun pm-section-setup-margin ()
  "Apply `pm-section-set-window-margin' to all windows showing this buffer.
Also adds it to the buffer-local `window-configuration-change-hook' so
future windows that display the buffer get the margin too."
  (dolist (window (get-buffer-window-list nil nil 0))
    (pm-section-set-window-margin window))
  (add-hook 'window-configuration-change-hook
            #'pm-section-set-window-margin nil t))

;;;; Section state reset

(defvar magit-section-pre-command-section)
(defvar magit-section-highlight-overlays)
(defvar magit-section-selection-overlays)
(defvar magit-section-highlighted-sections)
(defvar magit-section-focused-sections)
(defvar magit-root-section)

(defun pm-table-reset-section-state ()
  "Clear magit-section bookkeeping that would otherwise reference stale sections.

`erase-buffer' invalidates the markers inside every section object
left in these lists by the previous render — when
`magit-section-post-command-hook' iterates them the next time, the
stale objects can produce a nil and trip the hook with
`(wrong-type-argument ... nil)'.  Mirrors the reset block in
`magit-refresh-buffer' so our re-renders behave the same way."
  (when (boundp 'magit-section-pre-command-section)
    (setq magit-section-pre-command-section nil))
  (when (boundp 'magit-section-highlight-overlays)
    (setq magit-section-highlight-overlays nil))
  (when (boundp 'magit-section-selection-overlays)
    (setq magit-section-selection-overlays nil))
  (when (boundp 'magit-section-highlighted-sections)
    (setq magit-section-highlighted-sections nil))
  (when (boundp 'magit-section-focused-sections)
    (setq magit-section-focused-sections nil)))

(declare-function magit-section-show "magit-section" (section))

(defun pm-table-show-root-section ()
  "Walk from `magit-root-section' to populate visibility-indicator overlays.

The `magit-insert-section' macro builds section objects but does not
create their visibility-indicator overlays. Those overlays are made by
`magit-section-maybe-update-visibility-indicator', which only runs from
`magit-section-show' / `magit-section-hide'. Magit triggers it once per
refresh by calling `(magit-section-show magit-root-section)' (see
`magit-refresh-buffer' in magit-mode.el); pm mirrors that here."
  (when (and (boundp 'magit-root-section) magit-root-section)
    (let ((magit-section-cache-visibility nil))
      (magit-section-show magit-root-section))))

(defun pm-table-cover-root-section ()
  "Stamp `magit-section' on every char in the root section's region.

`magit-section--set-section-properties' explicitly skips the root
section, so any text in the root that's not inside a child (e.g. a
heading line, or a blank line between top-level children) carries
no `magit-section' property.  When such a position participates in
a region selection, `magit-region-sections' calls
`(magit-section-at rbeg)' → nil, then `magit-section-siblings nil'
→ `(oref nil parent)' → `(wrong-type-argument ... nil)'.

Run this as the last step of every render so every position in the
buffer resolves to a non-nil section."
  (when (and (boundp 'magit-root-section) magit-root-section)
    (let ((start (oref magit-root-section start))
          (end   (oref magit-root-section end))
          (props `(magit-section ,magit-root-section))
          (inhibit-read-only t))
      (save-excursion
        (goto-char start)
        (while (< (point) end)
          (let ((next (or (next-single-property-change (point) 'magit-section)
                          end)))
            (unless (get-text-property (point) 'magit-section)
              (add-text-properties (point) next props))
            (goto-char next)))))))

;;;; Section ancestor walk

(defun pm-section-ancestor-of-type (section types)
  "Walk SECTION ↦ parent ↦ ... ↦ root looking for a member of TYPES.

TYPES is a list of section-type symbols.  Returns the first
ancestor whose `type' slot is in TYPES, or nil."
  (let ((cur section))
    (while (and cur (not (memq (oref cur type) types)))
      (setq cur (oref cur parent)))
    cur))

;;;; Time formatting

(defun pm-table-format-iso (iso)
  "Trim ISO-8601 string ISO to `YYYY-MM-DD HH:MM' local time when parseable.
Falls back to the raw string on parse failure."
  (condition-case _err
      (format-time-string "%Y-%m-%d %H:%M" (date-to-time iso))
    (error iso)))

;;;; Rich-markup parser (for stacker `info' field)

(defconst pm-table--rich-style-faces
  '(("bold"     . bold)
    ("italic"   . italic)
    ("dim"      . pm-dim)
    ("blue"     . pm-agent-claude)
    ("green"    . pm-agent-codex)
    ("magenta"  . pm-agent-cursor)
    ("yellow"   . pm-row-warn)
    ("red"      . pm-row-error)
    ("cyan"     . pm-row-info))
  "Map a single Rich style word to an Emacs face.

Multi-word styles (e.g. \"bold blue\") are split and each word
mapped here; the resulting list of faces is applied so all
components compose.")

(defun pm-table--rich-faces (style-text)
  "Return a list of faces for STYLE-TEXT (a `[...]' tag's contents)."
  (delq nil
        (mapcar (lambda (word)
                  (cdr (assoc word pm-table--rich-style-faces)))
                (split-string style-text "[ \t]+" t))))

(defun pm-table-render-rich-markup (s)
  "Convert Rich markup S (e.g. \"[bold blue]name[/]\") to a propertized string.

Stack-based: each `[STYLE]' opens a frame, `[/]' or `[/STYLE]'
closes the most recent.  Unknown style words pass through unstyled.
`\\\\['/`\\\\]' literalizes brackets — Rich emits `\\\\[' for the
literal `[' character."
  (let ((i 0)
        (len (length s))
        (out (generate-new-buffer " *pm-rich*"))
        (stack nil))
    (unwind-protect
        (with-current-buffer out
          (while (< i len)
            (let ((ch (aref s i)))
              (cond
               ((and (eq ch ?\\) (< (1+ i) len) (eq (aref s (1+ i)) ?\[))
                (insert ?\[)
                (setq i (+ i 2)))
               ((and (eq ch ?\\) (< (1+ i) len) (eq (aref s (1+ i)) ?\]))
                (insert ?\])
                (setq i (+ i 2)))
               ((eq ch ?\[)
                (let ((close (string-match-p "\\]" s i)))
                  (if (not close)
                      (progn (insert ch) (setq i (1+ i)))
                    (let ((tag (substring s (1+ i) close)))
                      (cond
                       ((string-prefix-p "/" tag)
                        (when stack (pop stack)))
                       (t
                        (push (pm-table--rich-faces tag) stack)))
                      (setq i (1+ close))))))
               (t
                (let ((face-list
                       (apply #'append (reverse stack))))
                  (insert (propertize (string ch) 'face face-list)))
                (setq i (1+ i))))))
          (buffer-string))
      (kill-buffer out))))

(provide 'pm-table)

;;; pm-table.el ends here
