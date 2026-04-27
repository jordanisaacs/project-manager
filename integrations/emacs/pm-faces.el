;;; pm-faces.el --- Faces for pm magit-section buffers -*- lexical-binding: t -*-

;;; Commentary:

;; Face palette for the magit-section pm buffers.  The colors mirror the
;; CLI styles (defined in Python) so a worktree marked yellow in the
;; terminal stays yellow in Emacs:
;;
;;   src/project_manager/project/status.py  _KIND_STYLE
;;   src/project_manager/agent/ls.py        _AGENT_STYLE
;;   src/project_manager/stacker/render/graph.py  pr_icon
;;
;; Faces inherit from built-ins where possible (`shadow', `error',
;; `warning', `success') so they pick up the user's theme; explicit
;; `:foreground' colors are only set where there is no good built-in
;; (PR states, agent names, stacker repo).

;;; Code:

(require 'cl-lib)

(defgroup pm-faces nil
  "Faces for pm (project_manager) magit-section buffers."
  :group 'pm
  :group 'faces)

(defface pm-section-heading
  '((t :inherit magit-section-heading))
  "Section heading face (e.g., \"Worktrees\").
Inherits from `magit-section-heading' so the buffer blends with
the user's magit theme; override to taste."
  :group 'pm-faces)

(defface pm-group-heading
  '((t :inherit magit-section-secondary-heading))
  "Group heading face (e.g., a repo or project name within a section)."
  :group 'pm-faces)

(defface pm-row-active
  '((t :inherit success))
  "Worktree row marked ACTIVE / healthy.  Mirrors `_KIND_STYLE[ACTIVE]'."
  :group 'pm-faces)

(defface pm-row-warn
  '((t :inherit warning))
  "Worktree row marked DRIFT.  Mirrors `_KIND_STYLE[DRIFT]'."
  :group 'pm-faces)

(defface pm-row-error
  '((t :inherit error))
  "Worktree row marked STALE / BROKEN / ORPHAN.  Mirrors red kinds."
  :group 'pm-faces)

(defface pm-row-dim
  '((t :inherit shadow))
  "Worktree row marked DETACHED.  Mirrors `_KIND_STYLE[DETACHED]'."
  :group 'pm-faces)

(defface pm-row-info
  '((t :inherit font-lock-builtin-face))
  "Worktree row marked OPS_OWNED.  Mirrors `cyan' in `_KIND_STYLE'."
  :group 'pm-faces)

(defface pm-pr-open
  '((((class color) (background dark))  :foreground "green3")
    (((class color) (background light)) :foreground "ForestGreen"))
  "PR open / ready (cyan/green icon column)."
  :group 'pm-faces)

(defface pm-pr-draft
  '((t :inherit shadow))
  "PR in draft state."
  :group 'pm-faces)

(defface pm-pr-merged
  '((((class color) (background dark))  :foreground "MediumPurple1")
    (((class color) (background light)) :foreground "purple"))
  "Merged PR (matches the Rich `magenta' the CLI uses)."
  :group 'pm-faces)

(defface pm-pr-closed
  '((t :inherit error))
  "Closed-without-merge PR."
  :group 'pm-faces)

(defface pm-agent-claude
  '((((class color) (background dark))  :foreground "DeepSkyBlue1")
    (((class color) (background light)) :foreground "blue3"))
  "Claude agent label.  Mirrors `_AGENT_STYLE[claude]' (blue)."
  :group 'pm-faces)

(defface pm-agent-codex
  '((((class color) (background dark))  :foreground "green3")
    (((class color) (background light)) :foreground "ForestGreen"))
  "Codex agent label.  Mirrors `_AGENT_STYLE[codex]' (green)."
  :group 'pm-faces)

(defface pm-agent-cursor
  '((((class color) (background dark))  :foreground "MediumPurple1")
    (((class color) (background light)) :foreground "purple"))
  "Cursor agent label.  Mirrors `_AGENT_STYLE[cursor]' (magenta)."
  :group 'pm-faces)

(defface pm-stacker-repo
  '((t :inherit pm-group-heading :weight bold))
  "Repo name in stacker output (matches Rich `bold blue')."
  :group 'pm-faces)

(defface pm-dim
  '((t :inherit shadow))
  "Generic dim text (timestamps, secondary info)."
  :group 'pm-faces)

(defface pm-id
  '((t :inherit font-lock-constant-face))
  "Stable identifiers (session UUIDs, slot UUIDs)."
  :group 'pm-faces)

;;;; Mapping helpers

(defconst pm-faces--kind-alist
  '(("active"          . pm-row-active)
    ("detached"        . pm-row-dim)
    ("drift"           . pm-row-warn)
    ("stale"           . pm-row-error)
    ("broken"          . pm-row-error)
    ("orphan_forward"  . pm-row-error)
    ("orphan_owner"    . pm-row-error)
    ("ops_owned"       . pm-row-info))
  "Map a `check.Kind' string value (the JSON `kind' field) to a face.")

(defun pm-faces-kind (kind)
  "Return the face for a worktree KIND string, or `default' if unknown."
  (or (cdr (assoc kind pm-faces--kind-alist)) 'default))

(defconst pm-faces--agent-alist
  '(("claude" . pm-agent-claude)
    ("codex"  . pm-agent-codex)
    ("cursor" . pm-agent-cursor))
  "Map an agent name string (`claude'/`codex'/`cursor') to a face.")

(defun pm-faces-agent (agent)
  "Return the face for an AGENT name string, or `default' if unknown."
  (or (cdr (assoc agent pm-faces--agent-alist)) 'default))

(defun pm-faces-pr (pr)
  "Return the face for a PR plist (`alist' from JSON), or nil if no PR.

Mirrors the dispatch in `stacker.render.graph.pr_icon': merged
trumps closed trumps draft trumps open."
  (when pr
    (cond
     ((alist-get 'merged pr)            'pm-pr-merged)
     ((string= (alist-get 'state pr) "CLOSED") 'pm-pr-closed)
     ((alist-get 'is_draft pr)          'pm-pr-draft)
     (t                                  'pm-pr-open))))

(provide 'pm-faces)

;;; pm-faces.el ends here
