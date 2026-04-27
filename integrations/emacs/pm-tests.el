;;; pm-tests.el --- ERT tests for pm.el -*- lexical-binding: t -*-

;;; Commentary:

;; Run with:
;;   emacs -Q -batch -L . -l ert -l pm-tests.el \
;;     -f ert-run-tests-batch-and-exit
;;
;; All tests are hermetic — `pm--run-async' is mocked so no real `pm'
;; binary is invoked.

;;; Code:

(require 'ert)
(require 'cl-lib)
(let ((dir (file-name-directory (or load-file-name buffer-file-name))))
  (add-to-list 'load-path dir))
(require 'pm)
(require 'pm-process)
(require 'pm-project)
(require 'pm-commands)
(require 'pm-ui)
(require 'pm-transient)
(require 'pm-faces)
(require 'pm-table)

;; magit-section is required for `pm-status', `pm-list', `pm-pool',
;; `pm-repo', `pm-agent'.  Tests that touch those modules guard on it.
(defconst pm-test--has-magit-section
  (require 'magit-section nil t))
(when pm-test--has-magit-section
  (require 'pm-agent))

(defmacro pm-test--with-fixture (var &rest body)
  "Bind VAR to a temp pm-projects-dir and evaluate BODY.
The temp tree contains two containers, `alpha' (worktrees a, b) and
`beta' (worktree main).  Each worktree symlink points to a fake git
checkout dir under <tmp>/pool/<repo>/<uuid>."
  (declare (indent 1))
  `(let* ((,var (file-name-as-directory (make-temp-file "pm-test-" t)))
          (pool (file-name-as-directory (concat ,var "pool/")))
          (pm-projects-dir ,var)
          (pm-include-files-from-worktrees t))
     (unwind-protect
         (progn
           (make-directory (concat pool "repo1/uuid-a") t)
           (make-directory (concat pool "repo1/uuid-b") t)
           (make-directory (concat pool "repo2/uuid-m") t)
           (dolist (slot '("repo1/uuid-a" "repo1/uuid-b" "repo2/uuid-m"))
             (write-region "" nil (concat pool slot "/.git")))
           (make-directory (concat ,var "alpha/") t)
           (write-region "" nil (concat ,var "alpha/.pm.db"))
           (make-symbolic-link (concat pool "repo1/uuid-a")
                               (concat ,var "alpha/a"))
           (make-symbolic-link (concat pool "repo1/uuid-b")
                               (concat ,var "alpha/b"))
           (make-directory (concat ,var "beta/") t)
           (write-region "" nil (concat ,var "beta/.pm.db"))
           (make-symbolic-link (concat pool "repo2/uuid-m")
                               (concat ,var "beta/main"))
           ,@body)
       (delete-directory ,var t))))

(ert-deftest pm-test--project-finder-recognizes-container ()
  (pm-test--with-fixture root
    (let ((proj (pm--project-finder (concat root "alpha/"))))
      (should (eq (car proj) 'pm))
      (should (string= (cdr proj)
                       (file-name-as-directory (concat root "alpha")))))))

(ert-deftest pm-test--project-finder-rejects-non-container ()
  (pm-test--with-fixture root
    (should (null (pm--project-finder (concat root "alpha/a/"))))
    (should (null (pm--project-finder root)))))

(ert-deftest pm-test--project-name-from-container ()
  (pm-test--with-fixture root
    (let ((proj (cons 'pm (file-name-as-directory (concat root "alpha")))))
      (should (string= (project-name proj) "alpha")))))

(ert-deftest pm-test--container-of-walks-up ()
  (pm-test--with-fixture root
    (should (string= (pm--container-of (concat root "alpha/a/some/sub/"))
                     (file-name-as-directory (concat root "alpha"))))
    (should (string= (pm--container-of (concat root "beta/main/"))
                     (file-name-as-directory (concat root "beta"))))))

(ert-deftest pm-test--container-of-returns-nil-outside ()
  (pm-test--with-fixture root
    (should (null (pm--container-of "/tmp/")))))

(ert-deftest pm-test--worktree-alias-of ()
  (pm-test--with-fixture root
    (should (string= (pm--worktree-alias-of (concat root "alpha/a/x/y"))
                     "a"))
    (should (string= (pm--worktree-alias-of (concat root "beta/main"))
                     "main"))
    (should (null (pm--worktree-alias-of (concat root "alpha/"))))))

(ert-deftest pm-test--worktree-aliases-in ()
  (pm-test--with-fixture root
    (should (equal (sort (pm--worktree-aliases-in
                          (concat root "alpha/"))
                         #'string<)
                   '("a" "b")))))

(ert-deftest pm-test--siblings-of-excludes-self ()
  (pm-test--with-fixture root
    (let ((pm--known-projects nil))
      (should (equal (pm--siblings-of
                      (file-name-as-directory (concat root "alpha"))
                      "a")
                     '("b")))
      (should (null (pm--siblings-of
                     (file-name-as-directory (concat root "beta"))
                     "main"))))))

(ert-deftest pm-test--display-name-from-symlink-path ()
  (pm-test--with-fixture root
    (should (string= (pm--display-name-from-root
                      (concat root "alpha/a/"))
                     "alpha/a"))
    (should (null (pm--display-name-from-root (concat root "alpha/"))))))

(ert-deftest pm-test--display-name-from-slot-path ()
  (pm-test--with-fixture root
    (let ((pm--slot-to-display
           (list (cons (file-name-as-directory
                        (concat root "pool/repo1/uuid-a"))
                       "alpha/a"))))
      (should (string= (pm--display-name-from-root
                        (concat root "pool/repo1/uuid-a/"))
                       "alpha/a")))))

(ert-deftest pm-test--name-advice-passes-through-non-pm ()
  (pm-test--with-fixture root
    (let ((pm--slot-to-display nil))
      (cl-letf (((symbol-function 'project-root)
                 (lambda (_) "/tmp/unrelated/")))
        (should (string= (pm--name-advice (lambda (_) "unrelated") '(vc Git "/tmp/unrelated/"))
                         "unrelated"))))))

(ert-deftest pm-test--name-advice-renders-alias ()
  (pm-test--with-fixture root
    (cl-letf (((symbol-function 'project-root)
               (lambda (_) (concat root "alpha/a/"))))
      (should (string= (pm--name-advice (lambda (_) "uuid-a")
                                        '(vc Git "/whatever"))
                       "alpha/a")))))

(ert-deftest pm-test--reconcile-populates-cache ()
  (pm-test--with-fixture root
    (let ((rows '(((project . "alpha")
                   (worktrees . (((wt . "a") (repo . "repo1")
                                  (branch . "main") (status . "attached"))
                                 ((wt . "b") (repo . "repo1")
                                  (branch . "feat") (status . "attached")))))
                  ((project . "beta")
                   (worktrees . (((wt . "main") (repo . "repo2")
                                  (branch . "trunk") (status . "attached")))))))
          (project--list nil)
          (pm--known-projects nil)
          (pm--slot-to-display nil))
      (cl-letf (((symbol-function 'project-remember-project)
                 (lambda (pr &optional _no-write)
                   (push (list (project-root pr)) project--list)))
                ((symbol-function 'project-forget-project) #'ignore)
                ((symbol-function 'project--write-project-list) #'ignore))
        (let ((containers (pm--reconcile rows)))
          (should (= 2 (length containers)))
          (should (assoc (file-name-as-directory (concat root "alpha"))
                         pm--known-projects))
          (should (assoc (file-name-as-directory (concat root "beta"))
                         pm--known-projects))
          (should (= 3 (length pm--slot-to-display))))))))

(ert-deftest pm-test--build-spec-with-aliases ()
  (cl-letf (((symbol-function 'read-string)
             (lambda (prompt &rest _)
               (cond
                ((string-match-p "Alias for repo1" prompt) "")
                ((string-match-p "Alias for repo2" prompt) "feature")
                (t "")))))
    (should (string= (pm--build-spec '("repo1" "repo2"))
                     "repo1,feature:repo2"))))

(ert-deftest pm-test--strs-flattens-and-drops-nil ()
  (should (equal (pm--strs "a" nil "b" '("c" "d") nil "e")
                 '("a" "b" "c" "d" "e")))
  (should (equal (pm--strs '("--all"))
                 '("--all"))))

;;;; Cross-project entry to pm-project-dispatch
;;
;; The bound project is communicated to the transient via `:scope', not
;; via `default-directory': the latter unwinds the moment the entry
;; function returns and is gone by the time the user presses a suffix
;; key.  These tests capture the `:scope' that `transient-setup' is
;; called with to verify the entry function picks the right project
;; under each scenario.

(defmacro pm-test--capture-dispatch-scope (&rest body)
  "Run BODY with `transient-setup' stubbed to capture the :scope arg.
Returns the captured scope (path string) or nil."
  `(let (captured)
     (cl-letf (((symbol-function 'transient-setup)
                (lambda (&rest args)
                  (setq captured
                        (plist-get (cdddr args) :scope)))))
       ,@body
       captured)))

(ert-deftest pm-test--project-dispatch-scope-is-current-project ()
  "Inside project A with no prefix arg, scope resolves to A."
  (pm-test--with-fixture root
    (let* ((default-directory (concat root "alpha/a/"))
           (scope (pm-test--capture-dispatch-scope
                   (call-interactively #'pm-project-dispatch))))
      (should scope)
      (should (string= (pm--container-name scope) "alpha")))))

(ert-deftest pm-test--project-dispatch-scope-prefix-arg-prompts ()
  "Inside project A with prefix arg, scope is the chosen project (B)."
  (pm-test--with-fixture root
    (cl-letf (((symbol-function 'pm--read-project)
               (lambda (&rest _) "beta")))
      (let* ((default-directory (concat root "alpha/a/"))
             (scope (pm-test--capture-dispatch-scope
                     (let ((current-prefix-arg '(4)))
                       (call-interactively #'pm-project-dispatch)))))
        (should scope)
        (should (string= (file-name-as-directory scope)
                         (file-name-as-directory (concat root "beta"))))))))

(ert-deftest pm-test--project-dispatch-scope-from-outside-prompts ()
  "From a path not under pm-projects-dir, scope is the prompted project."
  (pm-test--with-fixture root
    (cl-letf (((symbol-function 'pm--read-project)
               (lambda (&rest _) "beta")))
      (let* ((default-directory "/tmp/")
             (scope (pm-test--capture-dispatch-scope
                     (call-interactively #'pm-project-dispatch))))
        (should scope)
        (should (string= (file-name-as-directory scope)
                         (file-name-as-directory (concat root "beta"))))))))

(ert-deftest pm-test--project-dispatch-honors-project-switch-override ()
  "Regression: `C-x p p <B> P' from outside any pm tree must scope to B.

In Emacs 28+, `project-switch-project' does NOT rebind
`default-directory' — it sets `project-current-directory-override'
buffer-locally and calls the chosen command.  The entry function
must therefore read the override; otherwise it falls back to the
original buffer's directory (which has no pm container) and
either prompts again or scopes to the wrong place."
  (pm-test--with-fixture root
    (let* ((default-directory "/tmp/")
           (project-current-directory-override
            (concat root "beta/"))
           (scope (pm-test--capture-dispatch-scope
                   (call-interactively #'pm-project-dispatch))))
      (should scope)
      (should (string= (file-name-as-directory scope)
                       (file-name-as-directory (concat root "beta")))))))

(ert-deftest pm-test--project-dispatch-override-wins-over-current-buffer ()
  "From a buffer in project A, `C-x p p <B> P' must scope to B (not A).

When `project-current-directory-override' is set (the user just
explicitly chose B via `C-x p p'), it must take precedence over
the buffer's own pm context."
  (pm-test--with-fixture root
    (let* ((default-directory (concat root "alpha/a/"))
           (project-current-directory-override
            (concat root "beta/"))
           (scope (pm-test--capture-dispatch-scope
                   (call-interactively #'pm-project-dispatch))))
      (should scope)
      (should (string= (file-name-as-directory scope)
                       (file-name-as-directory (concat root "beta")))))))

(ert-deftest pm-test--project-dispatch-suffix-survives-let-unwind ()
  "Regression: scope persists after the entry function's locals unwind.

Reproduces the bug where `C-x p p <project-B> P' (from project A)
ran a status suffix that targeted A: the entry function let-bound
`default-directory' but `transient-setup' returns immediately, so by
the time the user presses a suffix key the let has unwound.  Fix is
to pass the project as :scope, which the prefix object retains.

To make the test airtight, we simulate the unwind by capturing the
scope inside the stub then re-resolving the container *outside* any
let bindings — `default-directory' has reverted to the original
buffer-level value and only :scope can carry the answer."
  (pm-test--with-fixture root
    (cl-letf (((symbol-function 'pm--read-project)
               (lambda (&rest _) "beta")))
      (let* ((default-directory (concat root "alpha/a/"))
             captured-scope)
        (cl-letf (((symbol-function 'transient-setup)
                   (lambda (&rest args)
                     (setq captured-scope (plist-get (cdddr args) :scope)))))
          (let ((current-prefix-arg '(4)))
            (call-interactively #'pm-project-dispatch)))
        ;; default-directory has reverted to alpha/a/ here; only the
        ;; explicit scope can identify the bound project.
        (should captured-scope)
        (should (string= (file-name-as-directory captured-scope)
                         (file-name-as-directory (concat root "beta"))))))))

;;;; Rich-markup parser

(defun pm-test--rich (s)
  "Strip text properties from a parser output for equality checks."
  (substring-no-properties (pm-table-render-rich-markup s)))

(ert-deftest pm-test--rich-markup-passes-plain-text ()
  (should (string= (pm-test--rich "hello") "hello")))

(ert-deftest pm-test--rich-markup-strips-known-tags ()
  (should (string= (pm-test--rich "[bold blue]name[/]") "name"))
  (should (string= (pm-test--rich "[/]trailing-close[/]") "trailing-close"))
  (should (string= (pm-test--rich "a[bold]b[/]c") "abc")))

(ert-deftest pm-test--rich-markup-keeps-unknown-tags-as-no-style ()
  ;; Unknown style words don't break parsing — content passes through.
  (should (string= (pm-test--rich "[mystery]hi[/]") "hi")))

(ert-deftest pm-test--rich-markup-handles-escaped-brackets ()
  (should (string= (pm-test--rich "x\\[y\\]z") "x[y]z")))

(ert-deftest pm-test--rich-markup-applies-faces ()
  "Bytes inside a known tag carry the mapped face on a `face' property."
  (let* ((out (pm-table-render-rich-markup "[bold]A[/]B"))
         (face-on-A (get-text-property 0 'face out))
         (face-on-B (get-text-property 1 'face out)))
    (should (string= (substring-no-properties out) "AB"))
    (should (memq 'bold face-on-A))
    (should (or (null face-on-B) (equal face-on-B '())))))

;;;; Table grouping + width helpers

(ert-deftest pm-test--table-group-by-preserves-order ()
  (let* ((rows '(((repo . "a") (wt . "x"))
                 ((repo . "b") (wt . "y"))
                 ((repo . "a") (wt . "z"))))
         (groups (pm-table-group-by rows 'repo)))
    (should (equal (mapcar #'car groups) '("a" "b")))
    (should (= (length (cdr (assoc "a" groups))) 2))
    (should (= (length (cdr (assoc "b" groups))) 1))))

(ert-deftest pm-test--table-row-pads-non-trailing-cells ()
  (let* ((widths '(5 5 5))
         (line (pm-table-row '("a" "bb" "ccc") widths)))
    (should (string= line "a      bb     ccc"))))

;;;; Agent argv + dispatch

(when pm-test--has-magit-section
  (ert-deftest pm-test--agent-build-argv-claude ()
    (let ((pm-executable "pm"))
      (should (equal (pm-agent--build-argv "claude" "abc" "alpha")
                     '("pm" "agent" "claude" "--project" "alpha"
                       "--resume" "abc")))))

  (ert-deftest pm-test--agent-build-argv-codex-uses-subcommand ()
    "Codex takes `resume <id>' as a positional subcommand, not `--resume'."
    (let ((pm-executable "pm"))
      (should (equal (pm-agent--build-argv "codex" "xyz" "beta")
                     '("pm" "agent" "codex" "--project" "beta"
                       "resume" "xyz")))))

  (ert-deftest pm-test--agent-dispatch-default-stages-to-kill-ring ()
    "With `pm-agent-dispatch-function' nil, the command goes to the kill-ring."
    (let ((pm-agent-dispatch-function nil)
          (pm-projects-dir (file-name-as-directory (make-temp-file "pm-d-" t)))
          (pm-executable "pm")
          (kill-ring nil))
      (unwind-protect
          (progn
            (make-directory (concat pm-projects-dir "alpha/") t)
            (pm-agent--dispatch
             '(:agent "claude" :session-id "abc" :project "alpha"))
            (should (= 1 (length kill-ring)))
            (should (string-match-p "agent claude" (car kill-ring)))
            (should (string-match-p "--resume abc" (car kill-ring))))
        (delete-directory pm-projects-dir t))))

  (ert-deftest pm-test--agent-dispatch-funcalls-custom-function ()
    "When `pm-agent-dispatch-function' is set, it gets the enriched plist."
    (let* (captured
           (pm-agent-dispatch-function (lambda (plist) (setq captured plist)))
           (pm-projects-dir (file-name-as-directory (make-temp-file "pm-d-" t)))
           (pm-executable "pm"))
      (unwind-protect
          (progn
            (make-directory (concat pm-projects-dir "alpha/") t)
            (pm-agent--dispatch
             '(:agent "claude" :session-id "abc" :project "alpha"))
            (should captured)
            (should (equal (plist-get captured :agent) "claude"))
            (should (equal (plist-get captured :session-id) "abc"))
            (should (equal (plist-get captured :project) "alpha"))
            (should (listp (plist-get captured :argv)))
            (should (member "--resume" (plist-get captured :argv)))
            (should (string-prefix-p pm-projects-dir
                                     (plist-get captured :cwd))))
        (delete-directory pm-projects-dir t)))))

(provide 'pm-tests)

;;; pm-tests.el ends here
