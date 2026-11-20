import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";

// ESLint 9 flat config; replaces .eslintrc.json ("next/core-web-vitals") now that `next lint`
// is gone in Next 16. Run with `npm run lint` (eslint .).
export default defineConfig([
  ...nextVitals,
  {
    // eslint-plugin-react-hooks 7 (via eslint-config-next 16) adds React Compiler diagnostics.
    // The app does not use the React Compiler, so these stay off, as they were under Next 14;
    // rules-of-hooks and exhaustive-deps keep their old levels. See docs/FRONTEND_AUDIT.md.
    rules: {
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/refs": "off",
      "react-hooks/preserve-manual-memoization": "off",
    },
  },
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
]);
