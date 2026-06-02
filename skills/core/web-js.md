# Skill: Web / JavaScript / TypeScript Bugs

## Trigger keywords
javascript, typescript, react, vue, node, npm, webpack, vite, rollup,
eslint, babel, next.js, express, fastify, promise, async, await, cors,
memory leak, event listener, closure, prototype, hydration, SSR, CSR,
bundler, tree-shaking, esm, commonjs, type error, runtime error, V8

## Common bug patterns

### Async / Promise bugs
- Unhandled promise rejection: always `await` or `.catch()` — check for
  fire-and-forget `async` calls especially in loops and event handlers
- Race condition: two concurrent async operations mutating shared state.
  Use a mutex pattern, `Promise.all` with care, or serialise with a queue
- Missing `await`: function returns a Promise, caller uses the Promise
  object instead of its resolved value — TypeScript often catches this if
  strict mode is on

### Memory leaks (browser / Node)
Common sources:
1. Event listeners added but never removed (`removeEventListener` missing)
2. Closures capturing large objects in module scope
3. `setInterval` / `setTimeout` never cleared
4. React: `useEffect` with subscriptions missing cleanup return function
5. Node streams: not consuming or destroying on error

**Diagnostic**: Chrome DevTools Memory tab → Heap snapshot diff between
two points in time. Look for detached DOM nodes and growing listener counts.

### React-specific
- Stale closure in `useEffect`: dependency array missing a value that the
  effect reads — ESLint `exhaustive-deps` rule catches this
- Hydration mismatch: server-rendered HTML differs from client render.
  Caused by `Date.now()`, `Math.random()`, browser-only APIs in render
- Infinite re-render: `useEffect` dependency updates the state it depends on

### TypeScript type errors at runtime
TypeScript is erased at runtime. Common surprises:
- `as any` or `as unknown as T` casts hiding real type mismatches
- External API responses not validated — use `zod` or similar at boundaries
- Enums compiled to objects — `Object.keys(MyEnum)` includes numeric reverse mappings

### Node.js / server
- `EADDRINUSE`: port already in use — prior process still running
- `ECONNREFUSED`: downstream service not ready, missing retry/backoff
- `ERR_MODULE_NOT_FOUND`: ESM vs CJS mismatch — check `"type": "module"` in package.json and file extensions

## Useful debug approaches
```bash
# Node: verbose module resolution
NODE_DEBUG=module node app.js

# Node: inspect memory
node --inspect app.js   # then open chrome://inspect

# TypeScript: strict mode catches most runtime type bugs
# tsconfig.json: "strict": true

# Check for duplicate package versions causing hook/context issues
npm ls react
```

## Key test patterns
- Unit: Jest / Vitest with `@testing-library/react` for components
- Integration: Supertest for Express/Fastify routes
- E2E: Playwright or Cypress
- Always test error paths and rejected promises explicitly
