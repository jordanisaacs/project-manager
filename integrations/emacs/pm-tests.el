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
;; `pm-repo', `pm-agent', `pm-serve'.  Tests that touch those modules guard
;; on it.
(defconst pm-test--has-magit-section
  (require 'magit-section nil t))
(when pm-test--has-magit-section
  (require 'pm-status)
  (require 'pm-list)
  (require 'pm-pool)
  (require 'pm-repo)
  (require 'pm-agent)
  (require 'pm-serve)
  (require 'pm-stacker))

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

(ert-deftest pm-test--agent-launch-dispatch-scope-is-current-project ()
  "Inside project A, `pm-agent-launch-dispatch' scope resolves to A."
  (pm-test--with-fixture root
    (let* ((default-directory (concat root "alpha/a/"))
           (scope (pm-test--capture-dispatch-scope
                   (call-interactively #'pm-agent-launch-dispatch))))
      (should scope)
      (should (string= (pm--container-name scope) "alpha")))))

(ert-deftest pm-test--agent-launch-dispatch-honors-override ()
  "`pm-agent-launch-dispatch' must honor `project-current-directory-override'.
This is the path used by `project-switch-project' to communicate the
just-picked project to its post-switch command (`?a' under
`project-switch-commands')."
  (pm-test--with-fixture root
    (let* ((default-directory "/tmp/")
           (project-current-directory-override
            (concat root "beta/"))
           (scope (pm-test--capture-dispatch-scope
                   (call-interactively #'pm-agent-launch-dispatch))))
      (should scope)
      (should (string= (file-name-as-directory scope)
                       (file-name-as-directory (concat root "beta")))))))

(ert-deftest pm-test--agent-launch-dispatch-prefix-arg-prompts ()
  "Prefix arg forces a prompt even from inside a pm tree."
  (pm-test--with-fixture root
    (cl-letf (((symbol-function 'pm--read-project)
               (lambda (&rest _) "beta")))
      (let* ((default-directory (concat root "alpha/a/"))
             (scope (pm-test--capture-dispatch-scope
                     (let ((current-prefix-arg '(4)))
                       (call-interactively #'pm-agent-launch-dispatch)))))
        (should scope)
        (should (string= (file-name-as-directory scope)
                         (file-name-as-directory (concat root "beta"))))))))

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

(ert-deftest pm-test--propertize-face-sets-font-lock-face ()
  "`pm-propertize-face' must set BOTH `face' and `font-lock-face'.

`magit-section-mode' leaves font-lock enabled, and jit-lock strips
the plain `face' property on display — only `font-lock-face'
survives.  So every themed string must carry `font-lock-face' or it
renders unthemed (the bug behind \"I don't see any theming\").
Mirrors magit's own `magit--propertize-face'."
  (let ((s (pm-propertize-face "x" 'pm-row-warn)))
    (should (eq 'pm-row-warn (get-text-property 0 'face s)))
    (should (eq 'pm-row-warn (get-text-property 0 'font-lock-face s)))))

(ert-deftest pm-test--table-row-faced-matches-plain-text ()
  "`pm-table-row-faced' must lay out identically to `pm-table-row'.

Theming only adds `face' / `font-lock-face' text properties; the
visible characters (layout, padding, separators) must be
byte-for-byte the same so column alignment and substring lookups
are unaffected."
  (let* ((widths '(5 5 5))
         (cells '("a" "bb" "ccc"))
         (faces '(pm-row-warn nil pm-id))
         (faced (pm-table-row-faced cells widths faces)))
    (should (string= (substring-no-properties faced)
                     (pm-table-row cells widths)))
    ;; First cell ("a    ") carries its face on both properties; the
    ;; unfaced middle cell ("bb   ", at column 7) carries neither; the
    ;; faced last cell does.
    (should (eq 'pm-row-warn (get-text-property 0 'face faced)))
    (should (eq 'pm-row-warn (get-text-property 0 'font-lock-face faced)))
    (should (null (get-text-property 7 'face faced)))
    (should (null (get-text-property 7 'font-lock-face faced)))
    (should (eq 'pm-id (get-text-property (1- (length faced)) 'face faced)))
    (should (eq 'pm-id (get-text-property (1- (length faced))
                                          'font-lock-face faced)))))

(ert-deftest pm-test--table-row-faced-tolerates-short-faces ()
  "A FACES list shorter than CELLS treats the missing tail as nil."
  (let ((line (pm-table-row-faced '("a" "b" "c") '(3 3 3) '(pm-id))))
    (should (string= (substring-no-properties line)
                     (pm-table-row '("a" "b" "c") '(3 3 3))))
    (should (eq 'pm-id (get-text-property 0 'face line)))))

(ert-deftest pm-test--table-heading-dims-the-count ()
  "Section headings carry the label face on the label and `pm-count'
on the parenthesized count."
  (let ((h (pm-table-heading "Worktrees" 3)))
    (should (string= (substring-no-properties h) "Worktrees (3)"))
    (should (eq 'pm-section-heading (get-text-property 0 'face h)))
    (should (eq 'pm-section-heading (get-text-property 0 'font-lock-face h)))
    ;; The "(3)" tail is dimmed (on both face properties).
    (should (eq 'pm-count (get-text-property (1- (length h)) 'face h)))
    (should (eq 'pm-count (get-text-property (1- (length h)) 'font-lock-face h)))))

(ert-deftest pm-test--table-banner-optional-count ()
  "`pm-table-banner' appends a dim count only when one is given."
  (should (string= (substring-no-properties (pm-table-banner "Repos" 2))
                   "Repos (2)"))
  (should (string= (substring-no-properties (pm-table-banner "pm: demo"))
                   "pm: demo")))

(ert-deftest pm-test--faces-status-maps-known-tokens ()
  "`pm-faces-status' maps state tokens case-insensitively and returns
nil for free-form values (e.g. a claiming project name)."
  (should (eq 'pm-row-active (pm-faces-status "FREE")))
  (should (eq 'pm-row-active (pm-faces-status "free")))
  (should (eq 'pm-row-info   (pm-faces-status "OPS")))
  (should (eq 'pm-row-warn   (pm-faces-status "drift")))
  (should (eq 'pm-row-error  (pm-faces-status "stale")))
  (should (null (pm-faces-status "demo-project")))
  (should (null (pm-faces-status nil))))

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

  (ert-deftest pm-test--agent-build-argv-fresh-launch-omits-resume ()
    "A nil session-id builds a fresh-launch argv with no resume bits."
    (let ((pm-executable "pm"))
      (should (equal (pm-agent--build-argv "claude" nil "alpha")
                     '("pm" "agent" "claude" "--project" "alpha")))
      (should (equal (pm-agent--build-argv "codex" nil "beta")
                     '("pm" "agent" "codex" "--project" "beta")))
      (should (equal (pm-agent--build-argv "cursor" nil "gamma")
                     '("pm" "agent" "cursor" "--project" "gamma")))))

  (ert-deftest pm-test--agent-launch-routes-through-dispatcher ()
    "`pm-agent-launch' funcalls `pm-agent-dispatch-function' with a
fresh-launch plist (session-id nil) and the configured argv/cwd."
    (let* (captured
           (pm-agent-dispatch-function (lambda (plist) (setq captured plist)))
           (pm-projects-dir (file-name-as-directory (make-temp-file "pm-d-" t)))
           (pm-executable "pm"))
      (unwind-protect
          (progn
            (make-directory (concat pm-projects-dir "alpha/") t)
            (pm-agent-launch "claude" "alpha")
            (should captured)
            (should (equal (plist-get captured :agent) "claude"))
            (should (null (plist-get captured :session-id)))
            (should (equal (plist-get captured :project) "alpha"))
            (should (equal (plist-get captured :argv)
                           '("pm" "agent" "claude" "--project" "alpha")))
            (should (string-prefix-p pm-projects-dir
                                     (plist-get captured :cwd))))
        (delete-directory pm-projects-dir t))))

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

  ;;;; Magit-section render correctness
  ;;
  ;; Regression tests for `magit-section-post-command-hook' errors.
  ;; The hook walks `(magit-current-section)' and the
  ;; highlighted-/focused-sections lists; stale section objects (from
  ;; a previous render whose markers `erase-buffer' collapsed) plus a
  ;; nil section at point both produce the cryptic
  ;; `(wrong-type-argument (or eieio-object cl-structure-object
  ;; oclosure) nil)'.

  (defun pm-test--status-fixture-data ()
    "Minimal JSON-shaped status payload exercising every section."
    `((worktrees . (((wt . "main") (repo . "alpha")
                     (branch . "trunk") (kind . "active")
                     (pr . nil) (tracked . t) (detail . "healthy"))
                    ((wt . "feat") (repo . "alpha")
                     (branch . "f-x") (kind . "drift")
                     (pr . nil) (tracked . :json-false)
                     (detail . "branch ahead"))))
      (prs . (((wt . "main") (repo . "alpha") (branch . "trunk")
               (state . "OPEN") (is_draft . :json-false)
               (merged . :json-false) (is_approved . t)
               (has_open_comments . :json-false)
               (pr_url . "https://example.com/pr/1"))))
      (stacker . (((repo . "alpha")
                   (info . "[bold blue]alpha[/]\n└── trunk"))))
      (sessions . (((project . "demo") (agent . "claude")
                    (session_id . "abc-123") (title . "First")
                    (last_active . "2026-04-27T00:00:00+00:00"))
                   ((project . "demo") (agent . "codex")
                    (session_id . "def-456") (title . "Second")
                    (last_active . "2026-04-26T12:00:00+00:00"))))))

  (defmacro pm-test--with-rendered-status (&rest body)
    "Make a `pm-status-mode' buffer with fixture data, render, run BODY in it."
    (declare (indent 0))
    `(let ((buf (generate-new-buffer "*pm-test-status*")))
       (unwind-protect
           (with-current-buffer buf
             (pm-status-mode)
             (setq pm-status--project "demo")
             (setq pm-status--data (pm-test--status-fixture-data))
             (pm-status--render)
             ,@body)
         (kill-buffer buf))))

  (defun pm-test--every-pos-has-section-p ()
    "Return nil iff every buffer position resolves to a non-nil section.

`(magit-current-section)' falls back to `magit-root-section' so the
real check is at every position, the property is set OR the root is
non-nil — both contribute to a usable section.  This is what
prevents the post-command hook from observing a nil section."
    (save-excursion
      (cl-loop for p from (point-min) to (point-max)
               for cur = (progn (goto-char p) (magit-current-section))
               unless cur return p
               finally return nil)))

  (ert-deftest pm-test--status-render-covers-all-positions ()
    "Every buffer position must resolve to a non-nil current section.

Repro: pre-fix, the preamble inserted with raw `insert' had no
`magit-section' text property and `magit-root-section' was nil
between renders, so `(magit-current-section)' returned nil at the
top of the buffer."
    (pm-test--with-rendered-status
      (let ((bad-pos (pm-test--every-pos-has-section-p)))
        (should (null bad-pos)))))

  (ert-deftest pm-test--status-render-resets-stale-section-state ()
    "Re-render must clear `magit-section-highlighted-sections' /
`magit-section-focused-sections' so the post-command hook does not
iterate stale section objects whose markers collapsed in
`erase-buffer'.

Without the reset, the next post-command-hook trips with
`(wrong-type-argument ... nil)'."
    (pm-test--with-rendered-status
      ;; Simulate having highlighted state from a previous render.
      (setq magit-section-highlighted-sections
            (list (magit-current-section))
            magit-section-focused-sections
            (list (magit-current-section)))
      ;; Re-render; the helper must wipe the stale lists.
      (pm-status--render)
      (should (null magit-section-highlighted-sections))
      (should (null magit-section-focused-sections))))

  (ert-deftest pm-test--status-post-command-hook-no-error ()
    "Running the post-command hook at every buffer position must not error.

This is the direct regression for the symptom in `*Messages*`:
\"Error in post-command-hook (magit-section-post-command-hook):
\(wrong-type-argument (or eieio-object cl-structure-object oclosure)
nil)\".  We force `cursor-sensor-mode' off so the test doesn't need
a live frame, but the rest of the hook (focus + highlight) still
runs."
    (pm-test--with-rendered-status
      (let ((cursor-sensor-mode nil)
            errors)
        (cl-loop
         for p from (point-min) to (point-max) do
         (goto-char p)
         (condition-case err
             (run-hooks 'post-command-hook)
           (error (push (cons p err) errors))))
        (should-not errors))))

  (ert-deftest pm-test--status-render-covers-root-with-section-property ()
    "Every char inside the root section must carry `magit-section'.

Regression for the crash in `magit-region-sections':
  rbeg = position 1 (root heading text, no `magit-section' property)
  → `(magit-section-at rbeg)' returns nil
  → `(magit-section-siblings nil 'next)'
  → `(oref nil parent)' inside `and-let*'
  → `(wrong-type-argument (or eieio-object cl-structure-object oclosure) nil)'
`magit-section--set-section-properties' explicitly skips the root,
so heading text and any inter-child gap need explicit covering."
    (pm-test--with-rendered-status
      ;; `point-max' is one past the last char; the property at that
      ;; position is irrelevant since `magit-section-at' is only
      ;; called at positions of actual buffer text.
      (cl-loop
       for p from (point-min) below (point-max) do
       (should (get-text-property p 'magit-section)))))

  (ert-deftest pm-test--status-region-from-root-heading-no-error ()
    "`magit-region-sections' must not error when the region begins at point-min.

Direct repro of the live-daemon symptom.  When the user activates a
region that begins inside the root section's heading line and ends
inside a child section, magit-section's post-command hook
eventually calls `(magit-region-sections)', which calls
`(magit-section-at rbeg)'.  If rbeg has no `magit-section'
property (root-heading region pre-fix), that returns nil; then
`magit-section-siblings' calls `(oref nil parent)' inside
`and-let*' and signals
`(wrong-type-argument (or eieio-object cl-structure-object oclosure) nil)'."
    (pm-test--with-rendered-status
      (let ((inhibit-read-only t))
        (goto-char (point-min))
        (forward-line 5)
        (push-mark (point-min) t t)
        (activate-mark)
        ;; The hook itself calls this — call it directly to exercise
        ;; the exact failing path without depending on the hook's
        ;; cond branches firing in batch.
        (let (err)
          (condition-case e (magit-region-sections)
            (error (setq err e)))
          (should-not err)))))

  (ert-deftest pm-test--status-multiple-renders-no-stale-state ()
    "A render-erase-render cycle must not leave the post-command hook
walking sections from the previous render."
    (pm-test--with-rendered-status
      ;; First render already happened; now move point so the hook
      ;; populates highlight state, then re-render and walk again.
      (goto-char (point-min))
      (run-hooks 'post-command-hook)
      ;; Re-render with the same data.
      (pm-status--render)
      (let (errors)
        (cl-loop
         for p from (point-min) to (point-max) do
         (goto-char p)
         (condition-case err
             (run-hooks 'post-command-hook)
           (error (push (cons p err) errors))))
        (should-not errors))))

  (ert-deftest pm-test--agent-list-flattens-sectioned-json ()
    "`pm agent ls --json' returns sectioned data; the buffer renders rows.

Repro of the live symptom \"no sessions showing up\": the CLI emits
[{project, sessions: [...]}], but `pm-agent-list--render' iterates
its data as if it were a flat list of sessions.  Without
flattening, every cell looks up `agent'/`session_id' on the
sectioned wrapper, gets nil, and the buffer shows blank rows."
    (let ((groups
           '(((project . "alpha")
              (sessions . (((project . "alpha") (agent . "claude")
                            (session_id . "a1") (title . "first")
                            (last_active . "2026-01-01T00:00:00+00:00"))
                           ((project . "alpha") (agent . "codex")
                            (session_id . "a2") (title . "second")
                            (last_active . "2026-01-02T00:00:00+00:00")))))
             ((project . "beta")
              (sessions . (((project . "beta") (agent . "cursor")
                            (session_id . "b1") (title . "third")
                            (last_active . "2026-01-03T00:00:00+00:00"))))))))
      (let ((flat (pm-agent-list--flatten-groups groups)))
        (should (= 3 (length flat)))
        (should (equal "claude" (alist-get 'agent (nth 0 flat))))
        (should (equal "a1" (alist-get 'session_id (nth 0 flat))))
        (should (equal "alpha" (alist-get 'project (nth 0 flat))))
        (should (equal "beta" (alist-get 'project (nth 2 flat)))))))

  (ert-deftest pm-test--pool-list-renders-real-cli-fields ()
    "`*pm-pool*' must render the actual CLI fields (uuid/branch/status).

Repro of the live symptom \"data isn't rendering\": the buffer
columns were headed `UUID/Claim/Path' and the cell extractors
looked up `claim'/`path'.  The CLI's slot shape only carries
`repo'/`uuid'/`branch'/`status' — every cell came back empty.  We
mock `pm--pool-ls' with the real CLI shape and assert the buffer
text contains the slot's branch and status."
    (let ((buf (generate-new-buffer "*pm-test-pool*"))
          (groups
           '(((repo . "alpha")
              (slots . (((repo . "alpha")
                         (uuid . "11111111-2222-3333-4444-555555555555")
                         (branch . "feature-x")
                         (status . "demo-project"))))))))
      (unwind-protect
          (cl-letf (((symbol-function 'pm--pool-ls)
                     (lambda (_repo cb) (funcall cb groups))))
            (with-current-buffer buf
              (pm-pool-mode)
              (setq pm-pool--repo nil)
              (pm-pool-refresh)
              (let ((text (buffer-substring-no-properties
                           (point-min) (point-max))))
                (should (string-match-p "feature-x" text))
                (should (string-match-p "demo-project" text))
                (should (string-match-p "11111111" text))
                (should (string-match-p "alpha" text)))))
        (kill-buffer buf))))

  (ert-deftest pm-test--pool-slot-path-resolves-from-uuid-and-repo ()
    "`pm-pool--slot-path' builds <worktrees-dir>/<repo>/<uuid>.

Used by RET on a slot row to open the on-disk path in dired —
broken pre-fix because the renderer reached for a nonexistent
`path' field."
    (let ((pm-pool-worktrees-dir "/tmp/pm-pool-test/"))
      (should (string= (pm-pool--slot-path
                        '((repo . "alpha")
                          (uuid . "abc-uuid")
                          (branch . "main")
                          (status . "FREE")))
                       "/tmp/pm-pool-test/alpha/abc-uuid"))
      (should (null (pm-pool--slot-path '((branch . "main")))))))

  (ert-deftest pm-test--agent-list-no-prefix-defaults-to-all ()
    "`M-x pm-agent-list' with no prefix arg must default to `--all'.

The global agent-list buffer should always show sessions across
every project — the single-project view is reachable via
`pm-agent-list-current-project'.  Without a prefix arg, scope must
be `(nil . t)' (project nil, all t) so the CLI is invoked as
`pm agent ls --all --json'."
    (let (captured-scope captured-args)
      (cl-letf (((symbol-function 'pm--agent-ls)
                 (lambda (project all limit cb)
                   (setq captured-args (list project all limit))
                   (funcall cb '())))
                ((symbol-function 'pop-to-buffer) #'identity))
        (let ((current-prefix-arg nil))
          (call-interactively #'pm-agent-list))
        (when-let ((buf (get-buffer "*pm-agent-ls*")))
          (with-current-buffer buf
            (setq captured-scope pm-agent-list--scope))
          (kill-buffer buf)))
      (should (equal captured-scope (cons nil t)))
      (should (equal captured-args (list nil t nil)))))

  (ert-deftest pm-test--agent-list-buffer-name-scoped-by-project ()
    "Per-project agent lists must not share one buffer.

The all-projects view keeps the bare `*pm-agent-ls*' name; each
single-project view is suffixed with its project so opening
sessions for project A then project B yields two distinct
buffers instead of clobbering each other."
    ;; All-projects view (project nil, or all explicitly t).
    (should (equal (pm-agent-list--buffer-name nil t) "*pm-agent-ls*"))
    (should (equal (pm-agent-list--buffer-name nil nil) "*pm-agent-ls*"))
    ;; Per-project views are distinct from each other and from `all'.
    (should (equal (pm-agent-list--buffer-name "alpha" nil)
                   "*pm-agent-ls: alpha*"))
    (should (equal (pm-agent-list--buffer-name "beta" nil)
                   "*pm-agent-ls: beta*"))
    ;; An explicit ALL flag wins even when a project is named.
    (should (equal (pm-agent-list--buffer-name "alpha" t) "*pm-agent-ls*")))

  (ert-deftest pm-test--agent-list-prefix-prompts-for-project ()
    "With a prefix arg, `pm-agent-list' prompts for a project name.

Scope must be `(<chosen> . nil)' so the CLI runs with `--project
<chosen>' and no `--all'."
    (let (captured-scope captured-args)
      (cl-letf (((symbol-function 'pm--agent-ls)
                 (lambda (project all limit cb)
                   (setq captured-args (list project all limit))
                   (funcall cb '())))
                ((symbol-function 'pm--read-project)
                 (lambda (&rest _) "alpha"))
                ((symbol-function 'pop-to-buffer) #'identity))
        (let ((current-prefix-arg '(4)))
          (call-interactively #'pm-agent-list))
        ;; Per-project view gets a project-suffixed buffer name so it
        ;; doesn't clobber the all-projects view (see
        ;; `pm-agent-list--buffer-name').
        (when-let ((buf (get-buffer "*pm-agent-ls: alpha*")))
          (with-current-buffer buf
            (setq captured-scope pm-agent-list--scope))
          (kill-buffer buf)))
      (should (equal captured-scope (cons "alpha" nil)))
      (should (equal captured-args (list "alpha" nil nil)))))

  (ert-deftest pm-test--project-dispatch-sessions-uses-bound-project ()
    "`l' (\"sessions\") in `pm--project-dispatch-menu' must list sessions
for the project the transient was set up with (`(transient-scope)'),
not whatever project happens to contain `default-directory'.

Repro of the live symptom \"project-scoped agent session list won't
use that project (sometimes)\":

  1. user is in a buffer under project alpha.
  2. opens `pm-project-dispatch' with a prefix arg, picks beta.
     The transient is set up with `:scope' = beta's path.
  3. presses `l' to list beta's sessions.

The current wiring `(\"l\" \"sessions\" pm-agent-list-current-project)'
in `pm--project-dispatch-menu' ignores the bound project:
`pm-agent-list-current-project' looks at `pm-status--project' and
`(pm--container-of default-directory)', neither of which carries
the transient scope.  In step 3 it sees alpha (the original
buffer's container) and silently lists alpha's sessions.

Every other project-dispatch suffix (status, wt-add, wt-attach,
agent-launch, …) already reads `(pm--ds-container-name)' /
`(transient-scope)' for exactly this reason — see the comment
\"The bound project travels through the transient via `:scope'…\"
at the top of `pm-transient.el'.  This suffix was the one
oversight."
    (pm-test--with-fixture root
      (let ((captured-project 'unset)
            (default-directory (concat root "alpha/a/"))
            (beta-path (file-name-as-directory (concat root "beta"))))
        (cl-letf (((symbol-function 'pm--agent-ls)
                   (lambda (project _all _limit cb)
                     (setq captured-project project)
                     (funcall cb '())))
                  ((symbol-function 'pop-to-buffer) #'identity)
                  ((symbol-function 'pm-section-set-window-margin) #'ignore)
                  ((symbol-function 'transient-scope)
                   (lambda (&optional _) beta-path))
                  ((symbol-function 'pm--read-project)
                   (lambda (&rest _)
                     (error "must not prompt — scope is bound to beta"))))
          (unwind-protect
              (call-interactively #'pm-agent-list-current-project)
            (when-let ((buf (get-buffer "*pm-agent-ls: beta*")))
              (kill-buffer buf))))
        (should (equal captured-project "beta")))))

  (ert-deftest pm-test--agent-list-render-shows-rows-after-refresh ()
    "The agent-list buffer must show session rows after refresh.

Direct repro of \"no sessions showing up\": the CLI returns
sectioned JSON, `pm-agent-list-refresh' stores it on
`pm-agent-list--data', then renders.  Pre-fix, the renderer treated
the sectioned wrapper rows as session rows, looked up nonexistent
fields, and the buffer printed blank cells.  We mock
`pm--agent-ls' to feed the real CLI shape (a list of `(project,
sessions: [...])' groups) into the refresh path."
    (let ((buf (generate-new-buffer "*pm-test-agent*"))
          (sectioned
           '(((project . "alpha")
              (sessions . (((project . "alpha") (agent . "claude")
                            (session_id . "abc-123")
                            (title . "demo title")
                            (last_active . "2026-01-01T00:00:00+00:00"))))))))
      (unwind-protect
          (cl-letf (((symbol-function 'pm--agent-ls)
                     (lambda (_project _all _limit cb)
                       (funcall cb sectioned))))
            (with-current-buffer buf
              (pm-agent-list-mode)
              (setq pm-agent-list--scope (cons nil nil))
              (pm-agent-list-refresh)
              (let ((text (buffer-substring-no-properties
                           (point-min) (point-max))))
                (should (string-match-p "abc-123" text))
                (should (string-match-p "demo title" text))
                (should (string-match-p "claude" text)))))
        (kill-buffer buf))))

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

;;;; Stacker

(ert-deftest pm-test--stacker-status-faces ()
  "`pm-faces-stacker-status' maps tokens to (SYMBOL . FACE), mirroring graph.py."
  (should (equal (pm-faces-stacker-status "pr_open") '("●" . pm-pr-open)))
  (should (equal (pm-faces-stacker-status "pr_approved") '("✓" . pm-row-active)))
  (should (equal (pm-faces-stacker-status "pr_open_comments") '("⚑" . pm-row-error)))
  (should (equal (pm-faces-stacker-status "merged") '("■" . pm-pr-merged)))
  ;; no_pr has no color: cdr is ("○" . nil) == ("○").
  (should (equal (pm-faces-stacker-status "no_pr") '("○")))
  ;; Unknown / nil falls back to a dim dot.
  (should (equal (pm-faces-stacker-status "bogus") '("·" . pm-dim)))
  (should (equal (pm-faces-stacker-status nil) '("·" . pm-dim))))

(when pm-test--has-magit-section

  (defun pm-test--stacker-fixture ()
    "A two-repo `stacker' payload: a 2-deep arm + a merged solo + a paused op."
    '(((repo . "foo")
       (current_branch . "feat")
       (operation . ((op_type . "sync") (status . "paused")
                     (branch . "feat") (error_message . "cherry-pick conflict")))
       (branches
        . (((repo_name . "foo") (branch . "root") (parent_branch . "main")
            (is_root . t) (status . "pr_open") (commit_count . 2)
            (needs_sync . t) (ahead_of_remote . 1)
            (pr_url . "https://example.com/1") (merged . nil)
            (children
             . (((repo_name . "foo") (branch . "feat") (parent_branch . "root")
                 (is_root . nil) (status . "pr_approved") (commit_count . 1)
                 (needs_sync . nil) (ahead_of_remote . 0)
                 (pr_url . "https://example.com/2") (merged . nil)
                 (children . nil))))))))
      ((repo . "bar")
       (current_branch . nil)
       (operation . nil)
       (branches
        . (((repo_name . "bar") (branch . "solo") (parent_branch . "main")
            (is_root . t) (status . "merged") (commit_count . 0)
            (needs_sync . nil) (ahead_of_remote . 0)
            (pr_url . nil) (merged . t) (children . nil)))))))

  (defmacro pm-test--with-rendered-stacker (&rest body)
    "Render the stacker fixture in a `pm-stacker-mode' buffer, run BODY in it."
    (declare (indent 0))
    `(let ((buf (generate-new-buffer "*pm-test-stacker*")))
       (unwind-protect
           (with-current-buffer buf
             (pm-stacker-mode)
             (setq pm-stacker--project "demo")
             (setq pm-stacker--data (pm-test--stacker-fixture))
             (pm-stacker--render)
             ,@body)
         (kill-buffer buf))))

  (ert-deftest pm-test--stacker-render-tree ()
    "The stacker tree renders repos, nested branches, icons, markers, and the
paused-op banner — and every position resolves to a non-nil section."
    (pm-test--with-rendered-stacker
      (let ((text (buffer-substring-no-properties (point-min) (point-max))))
        (should (string-match-p "stacker: demo" text))
        (dolist (s '("foo" "bar" "root" "feat" "solo"))
          (should (string-match-p s text)))
        ;; Status icons (pr_open / pr_approved / merged).
        (dolist (icon '("●" "✓" "■"))
          (should (string-match-p icon text)))
        ;; Commit-group markers.
        (should (string-match-p "(2)" text))   ; commit_count
        (should (string-match-p "!" text))     ; needs_sync
        (should (string-match-p "↑1" text))    ; ahead_of_remote
        ;; Paused-operation banner.
        (should (string-match-p "paused" text))
        (should (string-match-p "cherry-pick conflict" text)))
      (should-not (pm-test--every-pos-has-section-p))))

  (ert-deftest pm-test--stacker-act-at-point-scopes-to-branch ()
    "RET on a branch sets the action transient's `:scope' to that branch."
    (pm-test--with-rendered-stacker
      (goto-char (point-min))
      (should (search-forward "feat" nil t))
      (let ((scope (pm-test--capture-dispatch-scope
                    (pm-stacker-act-at-point))))
        (should (equal (plist-get scope :project) "demo"))
        (should (equal (plist-get scope :repo) "foo"))
        (should (equal (plist-get scope :branch) "feat")))))

  (ert-deftest pm-test--stacker-wrapper-argv ()
    "The stacker wrappers build `pm stacker <verb> … --repo R --json' argv."
    (let (argv)
      (cl-letf (((symbol-function 'pm--run-async)
                 (lambda (args &rest _) (setq argv args))))
        (pm--stacker-push "kms" "feat" nil nil nil #'ignore)
        (should (equal argv '("stacker" "push" "feat" "--repo" "kms" "--json")))
        ;; `--all' drops the positional branch (the caller passes nil).
        (pm--stacker-sync "kms" nil t t nil nil #'ignore)
        (should (equal argv
                       '("stacker" "sync" "--repo" "kms" "--all" "--offline" "--json")))
        (pm--stacker-remove "kms" "feat" t nil t #'ignore)
        (should (equal argv
                       '("stacker" "remove" "feat" "--repo" "kms"
                         "--parent" "--force" "--json")))
        (pm--stacker-rename "kms" "feat" "feat2" #'ignore)
        (should (equal argv
                       '("stacker" "rename" "feat2" "--branch" "feat"
                         "--repo" "kms" "--json")))
        (pm--stacker-pr-unlink "kms" nil t #'ignore)
        (should (equal argv
                       '("stacker" "pr" "unlink" "--all" "--repo" "kms" "--json")))))))

;;;; pm-serve sidebar

(when pm-test--has-magit-section
  (ert-deftest pm-test--serve-filter-query-scopes-to-instance ()
    "The SSE filter pins SOURCE and the launching Emacs instance (EMACS)."
    (let* ((pm-agent-serve-source "emacs")
           (q (pm-serve--filter-query)))
      (should (string-match-p "meta\\.SOURCE=emacs" q))
      (should (string-match-p (format "meta\\.EMACS=%d" (emacs-pid)) q))))

  (ert-deftest pm-test--serve-session-key ()
    "Sessions are keyed by agent + NUL + vendor id; missing fields are empty."
    (should (equal (pm-serve--session-key '((agent . "claude") (vendor_session_id . "s1")))
                   (concat "claude" "\0" "s1")))
    (should (equal (pm-serve--session-key '((agent . "codex") (vendor_session_id . "x")))
                   (concat "codex" "\0" "x")))
    (should (equal (pm-serve--session-key '()) (concat "" "\0" ""))))

  (ert-deftest pm-test--serve-handle-snapshot-populates ()
    "A snapshot clears and repopulates the session table."
    (cl-letf (((symbol-function 'pm-serve--render) #'ignore))
      (let ((pm-serve--sessions (make-hash-table :test 'equal)))
        (puthash "stale" '((agent . "x")) pm-serve--sessions)
        (pm-serve--handle
         (concat "{\"type\":\"snapshot\",\"sessions\":["
                 "{\"agent\":\"claude\",\"vendor_session_id\":\"s1\"},"
                 "{\"agent\":\"codex\",\"vendor_session_id\":\"s2\"}]}"))
        (should (= 2 (hash-table-count pm-serve--sessions)))
        (should (gethash (concat "claude" "\0" "s1") pm-serve--sessions))
        (should (gethash (concat "codex" "\0" "s2") pm-serve--sessions))
        (should-not (gethash "stale" pm-serve--sessions)))))

  (ert-deftest pm-test--serve-handle-update-upserts ()
    "An update inserts then replaces the same key in place (no duplicates)."
    (cl-letf (((symbol-function 'pm-serve--render) #'ignore))
      (let ((pm-serve--sessions (make-hash-table :test 'equal))
            (key (concat "claude" "\0" "s1")))
        (pm-serve--handle
         "{\"type\":\"update\",\"session\":{\"agent\":\"claude\",\"vendor_session_id\":\"s1\",\"status\":\"working\"}}")
        (should (equal (alist-get 'status (gethash key pm-serve--sessions)) "working"))
        (pm-serve--handle
         "{\"type\":\"update\",\"session\":{\"agent\":\"claude\",\"vendor_session_id\":\"s1\",\"status\":\"idle\"}}")
        (should (= 1 (hash-table-count pm-serve--sessions)))
        (should (equal (alist-get 'status (gethash key pm-serve--sessions)) "idle")))))

  (ert-deftest pm-test--serve-handle-remove-deletes ()
    "A remove event drops the session keyed by its top-level agent/vendor id."
    (cl-letf (((symbol-function 'pm-serve--render) #'ignore))
      (let ((pm-serve--sessions (make-hash-table :test 'equal)))
        (pm-serve--handle
         "{\"type\":\"update\",\"session\":{\"agent\":\"claude\",\"vendor_session_id\":\"s1\"}}")
        (should (= 1 (hash-table-count pm-serve--sessions)))
        (pm-serve--handle "{\"type\":\"remove\",\"agent\":\"claude\",\"vendor_session_id\":\"s1\"}")
        (should (= 0 (hash-table-count pm-serve--sessions))))))

  (ert-deftest pm-test--serve-fit-pads-and-truncates ()
    "`pm-serve--fit' returns exactly WIDTH chars: pad short, ellipsize long."
    (let ((short (pm-serve--fit "hi" 6)))
      (should (= (length short) 6))
      (should (string-prefix-p "hi" short)))
    (should (= (length (pm-serve--fit "abcdefghij" 5)) 5))))

(provide 'pm-tests)

;;; pm-tests.el ends here
