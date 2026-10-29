# Vendored files and local changes

Source: <https://github.com/MIC-DKFZ/nninteractive-agent>, commit
`57b06cb4bac0aedaf4946b8f4a65247d998eff6e`, directory
`src/nninteractive_agent/web/static/`.

| File | Upstream path | SHA-256 of the upstream file | Modified |
|---|---|---|---|
| `viewer.js` | `viewer.js` | `c0ddc70e8805a6e3b37e34cd01766f2d139a27ddb22faa524bb8ba2f89b90397` | yes, see below |
| `render3d.js` | `render3d.js` | `04d680fcb383f064cab8078150c0c87ef10bd6b650648a9ab8985fddaaabf556` | no |
| `coords.js` | `coords.js` | `ee81e63adec244c9c4449bc2318f239befd825633b4ca7f64ed71e7670650ede` | no |
| `vendor/niivue-0.69.0.esm.js` | `vendor/niivue-0.69.0.esm.js` | `bcb456b66a26e20126b7257c2b6071f48b347b7f910f99bca26721a71a96cbed` | no |
| `vendor/LICENSE-niivue` | `vendor/LICENSE-niivue` | | no |
| `LICENSE-nninteractive-agent` | `LICENSE` (repository root) | | no |

`nni_viewer.jsx` is not vendored: it is this package's React wrapper.

## Changes

Every change made to a vendored file is listed here with its reason. Changed
lines carry a `reflex-nninteractive-viewer:` comment.

### `viewer.js`: `destroy()` for React unmount

Upstream creates its `ResizeObserver` and its two `window` mouse listeners as
anonymous objects, so a host page cannot release them. In a React app the
viewer is mounted and unmounted repeatedly (page navigation, conditional
rendering); each leaked viewer would keep its WebGL contexts and volumes alive.

1. `init()`: the `ResizeObserver` is kept in `this._resizeObserver`.
2. `_installInteraction()`: the `window` `mousemove`/`mouseup` handlers are kept
   in `this._winMove` / `this._winUp`.
3. New method `destroy()` at the end of the class: invalidates pending loads,
   disconnects the observer, removes the window listeners, cancels pending
   animation frames and timers (`_wlFrame`, `_wheelTimer`, `r3._frame`), calls
   NiiVue's `cleanup()` and loses both WebGL2 contexts.

Behaviour is otherwise unchanged. SHA-256 after the patch:
`28da6d51c7769ae79a10d2b89578b2f38b65aa71b56b16c78c7d75351fabb79e`.
