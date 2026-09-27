/**
 * econ-hello — the smallest possible host plugin for the `econ-harvester` preset.
 *
 * It exists to prove the wiring end to end: when a session mounts the preset,
 * cordis activates this row and calls `apply()` once, printing the marker to the
 * terminal that booted DSH.
 *
 * Plain ESM JavaScript on purpose. The cordis loader imports a plugin file
 * directly and declares no TypeScript toolchain (its only dependency is
 * `@deepseek-ai/cosmokit`), so a `.ts` row would fail to load. `type: "module"`
 * in the project's package.json is what makes this file ESM — do not remove it.
 */

/** Cordis reads the exported `name` as this plugin's identity in the tree. */
export const name = 'econ-hello'

/**
 * Hard service dependencies. Deliberately empty: this row must activate in any
 * host, including a preset that mounts no other row.
 */
export const inject = []

/** The marker this plugin prints, so the preset and the grep target agree. */
const MARKER = '[econ-hello]'

/**
 * Called when the preset's composition mounts, and again for each new
 * generation of that mount.
 *
 * @param ctx - the preset-scoped cordis context.
 */
export function apply(ctx) {
  console.log(`${MARKER} plugin applied — econ-harvester preset mounted`)

  // `ctx.effect` binds teardown to the mount's lifetime, so an unmount prints a
  // matching line instead of leaving the log unbalanced.
  ctx.effect(
    () => () => console.log(`${MARKER} plugin disposed — preset unmounted`),
    'econ-hello lifetime',
  )
}
