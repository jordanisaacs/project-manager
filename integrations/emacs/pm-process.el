;;; pm-process.el --- Async runner for pm CLI -*- lexical-binding: t -*-

;;; Commentary:

;; Async invocation of the `pm' CLI with --json output.  All shellouts
;; go through `pm--run-async'; success delivers a parsed alist to the
;; caller, failure surfaces stderr via `display-warning' and a kept
;; buffer.  An in-flight registry serializes mutating ops so transient
;; suffixes can disable themselves while a call is pending.

;;; Code:

(require 'cl-lib)
(require 'subr-x)

(defvar pm-executable)

(defvar pm--executable-cache nil
  "Cached resolved path to the `pm' binary.")

(defvar pm--inflight nil
  "Alist of (TAG . PROCESS) for pm calls in flight.
TAG is a symbol like `create' or `delete'; predicates use it to
gate transient suffixes during mutating ops.")

(defun pm--executable ()
  "Return the absolute path to the `pm' binary, resolving lazily."
  (or pm--executable-cache
      (setq pm--executable-cache
            (or (executable-find pm-executable)
                (user-error "pm: executable %S not found on exec-path"
                            pm-executable)))))

(defun pm--verb-tag (args)
  "Build a short tag like `project-status' from ARGS for buffer naming."
  (mapconcat #'identity (seq-take args 2) "-"))

(defun pm--out-buffer (verb)
  "Return (or create) the stdout buffer for VERB."
  (get-buffer-create (format " *pm-out: %s*" verb)))

(defun pm--err-buffer (verb)
  "Return (or create) the stderr buffer for VERB."
  (get-buffer-create (format "*pm: %s*" verb)))

(defun pm--surface-error (verb err-buf exit-code)
  "Surface a non-zero pm result for VERB from ERR-BUF with EXIT-CODE."
  (let ((summary (with-current-buffer err-buf
                   (goto-char (point-min))
                   (if (re-search-forward "^pm: \\(.*\\)$" nil t)
                       (match-string 1)
                     (string-trim
                      (buffer-substring-no-properties
                       (point-min) (min (point-max) 200)))))))
    (display-warning
     'pm
     (format "[%s] exit %d: %s" verb exit-code
             (if (string-empty-p summary) "<no stderr>" summary))
     :warning)))

(defun pm--parse-json (out-buf)
  "Parse JSON in OUT-BUF.  Returns a Lisp value or nil on empty input."
  (with-current-buffer out-buf
    (goto-char (point-min))
    (when (re-search-forward "[^[:space:]]" nil t)
      (goto-char (point-min))
      (json-parse-buffer :object-type 'alist
                         :array-type 'list
                         :null-object nil
                         :false-object nil))))

(cl-defun pm--run-async (args on-success
                              &key on-error tag input)
  "Invoke `pm' with ARGS asynchronously, calling ON-SUCCESS with parsed JSON.

ARGS is a list of strings (do not include the executable).  On exit 0,
ON-SUCCESS is called with the parsed JSON value (alist / list / nil).
On non-zero, ON-ERROR is called with (verb err-buffer exit-code) — or
`pm--surface-error' if ON-ERROR is omitted.

:TAG, when provided, registers the process in `pm--inflight' under that
symbol while running; predicates can use `pm--inflight-p' to gate UI.
:INPUT is an optional string written to the process's stdin."
  (let* ((bin (pm--executable))
         (verb (pm--verb-tag args))
         (out-buf (pm--out-buffer verb))
         (err-buf (pm--err-buffer verb)))
    (with-current-buffer out-buf (erase-buffer))
    (with-current-buffer err-buf (erase-buffer))
    (let* ((proc
            (make-process
             :name (format "pm-%s" verb)
             :buffer out-buf
             :stderr err-buf
             :command (cons bin args)
             :noquery t
             :sentinel
             (lambda (proc _event)
               (when (memq (process-status proc) '(exit signal))
                 (when tag
                   (setq pm--inflight (assq-delete-all tag pm--inflight)))
                 (let ((code (process-exit-status proc)))
                   (if (zerop code)
                       (condition-case err
                           (funcall on-success (pm--parse-json out-buf))
                         (error
                          (display-warning
                           'pm (format "[%s] callback error: %S" verb err)
                           :error)))
                     (funcall (or on-error #'pm--surface-error)
                              verb err-buf code))))))))
      (when tag
        (push (cons tag proc) pm--inflight))
      (when input
        (process-send-string proc input)
        (process-send-eof proc))
      proc)))

(defun pm--inflight-p (tag)
  "Return non-nil if a call tagged TAG is currently in flight."
  (and (assq tag pm--inflight) t))

(provide 'pm-process)

;;; pm-process.el ends here
