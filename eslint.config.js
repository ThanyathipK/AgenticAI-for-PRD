import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  {
    files: ["scripts/**/*.mjs"],
    languageOptions: {
      globals: {
        console: "readonly",
        process: "readonly",
      },
    },
  },
  {
    files: ["src/**/*.{ts,tsx}", "scripts/**/*.ts"],
    languageOptions: {
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    rules: {
      // TypeScript already checks unused values with project-aware settings.
      "@typescript-eslint/no-unused-vars": "off",
      // Legacy API response boundaries still require gradual `any` removal.
      "@typescript-eslint/no-explicit-any": "off",
      // Existing components intentionally synchronize server/UI state in effects.
      // Reworking those lifecycles is behavioral work, not a lint migration.
      "react-hooks/set-state-in-effect": "off",
      // Stable latest-value refs back asynchronous callbacks throughout the app.
      "react-hooks/refs": "off",
    },
  },
);
