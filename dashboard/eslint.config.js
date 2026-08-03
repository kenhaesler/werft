import js from '@eslint/js';
import svelte from 'eslint-plugin-svelte';
import svelteParser from 'svelte-eslint-parser';
// typescript-eslint resolves "typescript" via plain node module resolution;
// this project aliases that name to a 6.x-compatible shim so lint tooling
// works against TypeScript 7 - see README.md ("A note on the TypeScript pin").
import tseslint from 'typescript-eslint';
import globals from 'globals';

export default [
  {
    ignores: ['dist/**', 'node_modules/**'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs['flat/recommended'],
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.es2021,
      },
    },
  },
  {
    files: ['**/*.svelte'],
    languageOptions: {
      parser: svelteParser,
      parserOptions: {
        parser: tseslint.parser,
        extraFileExtensions: ['.svelte'],
      },
    },
  },
  {
    // The security floor (SPEC S7): run-derived strings must never be
    // rendered as HTML. This must stay an error, not a warning.
    rules: {
      'svelte/no-at-html-tags': 'error',
    },
  },
];
