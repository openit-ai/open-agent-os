import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  {
    ignores: [".next/**", "node_modules/**"],
  },
  {
    files: [
      "components/admin/**/*.{ts,tsx}",
      "lib/admin-api/**/*.{ts,tsx}",
      "lib/i18n/catalog.test.ts",
      "lib/setup-session.ts",
      "test/**/*.{ts,tsx}",
      "app/layout.tsx",
      "app/login/page.tsx",
      "app/(dashboard)/layout.tsx",
      "app/(dashboard)/page.tsx",
      "app/(dashboard)/setup/**/*.{ts,tsx}",
    ],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      "react-hooks": reactHooks,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
];
