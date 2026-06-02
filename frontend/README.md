# ClawsomeFlow Frontend

React 18 + TypeScript + Vite + Tailwind. Single-page app for the
ClawsomeFlow platform.

## Dev

Backend must be running on the configured `csflow_port` (default 17017):

```bash
# In another shell:
cd ../backend && python -m uvicorn app.main:app --port 17017

# Then:
npm install
npm run dev          # Vite at http://127.0.0.1:5173
```

The dev server proxies `/api` + `/ws` + `/health` to `127.0.0.1:17017`.

## Build

```bash
npm run build        # → dist/
npm run preview      # serve the built bundle locally
```

## Routes

| Path                    | Page              |
|-------------------------|-------------------|
| `/flows`                | Flow list         |
| `/flows/new`            | New flow          |
| `/flows/:id`            | Edit flow         |
| `/runs`                 | Run list          |
| `/runs/:id`             | Run detail (events stream + ClawTeam Board iframe + pending merges) |
| `/agents`               | OpenClaw agents   |
| `/agents/:id/chat`      | Direct chat with one agent |
| `/chat`                 | Chat picker       |
| `/profiles`             | Profiles list / show / test |

## Design conventions

- **No persistent agent list in the Flow editor** — agents are auto-derived
  from each task's `(ownerKind, ownerId, repo)` tuple. Users only think
  about tasks; the backend gets a complete spec on submit.
- **Leader hard-locked to its summary task only.** Non-summary tasks may
  not pick the leader as owner; the UI surfaces this inline + the
  backend validator (`LEADER_OWNS_WORKER_TASK`) rejects bypass attempts.
- **Owner kinds in the UI:** `openclaw`, `claude`, `codex`, `cursor`,
  `hermes`.
- **All wire payloads use camelCase** to match Pydantic `to_camel`
  serialisation — no per-call case conversion.
- **WebSocket auto-reconnect** with `sinceId` backfill so a transient
  disconnect doesn't drop events from the user's view.

## Internationalisation (i18n)

The UI ships with **two languages — English (default) and Chinese** —
selectable via the pill in the top bar. Persisted to localStorage and
applied to `<html lang>` immediately on switch (no page reload).

Stack: `i18next` + `react-i18next` + `i18next-browser-languagedetector`.

**Where things live:**

```
src/i18n/
  ├── index.ts        # i18next init + helpers (changeLang / currentLang)
  ├── en.ts           # English dictionary (default — the canonical source)
  ├── zh.ts           # Chinese dictionary (mirror; key-for-key equal)
  └── types.ts        # TS augmentation for typed `t('namespace.key')`
src/components/LanguageSwitcher.tsx   # the EN ⇄ 中文 pill
```

**Adding a string:**

1. Add the key to `src/i18n/en.ts` under the appropriate namespace.
2. Mirror it in `src/i18n/zh.ts` — `tsc -b --noEmit` fails the build
   on missing keys because both files share the same TS shape.
3. Use it in components: `const { t } = useTranslation(); t('flowList.title')`.
4. For interpolation: `t('shell.backendOnline', { version: '1.0.0' })`
   referencing `{{version}}` placeholder in the dictionary.

**Conventions:**
- Namespaces follow page or shared-area structure (`common`, `nav`,
  `shell`, `flowList`, `flowEditor.taskFields.*`, `runDetail`, etc.).
- Reuse `common.*` for plain words (save / cancel / back / close).
- Status enum labels live under `statusLabel.*` and `<StatusPill>`
  falls back to the raw value if a key is missing — safe for ad-hoc
  status strings the backend may add later.
- **Detection: `localStorage["csflow-lang"]` only.** We deliberately do
  NOT read `navigator.language` — every first-time visitor lands in
  English regardless of browser locale; the choice they make on the
  switcher is honoured forever after via localStorage.
