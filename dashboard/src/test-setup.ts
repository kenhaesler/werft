// Registers automatic DOM cleanup between tests (unmounts the previous
// render's component tree from document.body) so component tests don't leak
// elements into one another.
import '@testing-library/svelte/vitest';
