/// <reference types="vite/client" />

/** Injected at build time from the repository VERSION file (ADR-009). */
declare const __APP_VERSION__: string;

interface ImportMetaEnv {
  /**
   * Base URL of the PlummyKnowledge API. Public, build-time value — never put secrets in VITE_* variables:
   * they are embedded into the JavaScript bundle.
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
