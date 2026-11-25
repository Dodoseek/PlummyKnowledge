/** Why a document was not indexed, by the pipeline's stable error code (the code is shown if unknown). */
const INGESTION_ERRORS: Record<string, string> = {
  unsupported_media_type: 'The file type is not supported.',
  malformed_file: 'The file is damaged or not what its type says.',
  encrypted_file: 'The file is password-protected.',
  macro_enabled: 'Files with macros are not accepted.',
  archive_limits_exceeded: 'The file unpacks to more data than allowed.',
  invalid_text_encoding: 'The text is not valid UTF-8.',
  empty_file: 'The file is empty.',
  original_missing: 'The uploaded file is no longer available. Upload it again.',
  no_text: 'No text could be extracted.',
  parse_failed: 'The file could not be read.',
  parse_timeout: 'Reading the file took too long.',
  parse_resource_limit: 'Reading the file needed too much memory.',
  page_limit_exceeded: 'The document has too many pages.',
  sheet_limit_exceeded: 'The workbook has too many sheets.',
  row_limit_exceeded: 'The workbook has too many rows.',
  text_limit_exceeded: 'The document has too much text.',
  chunk_limit_exceeded: 'The document is too large to index.',
  extraction_missing: 'The extracted text is gone. Upload the file again.',
  extraction_invalid: 'The extracted text could not be read.',
  embedding_profile_unavailable: 'The embedding profile of the knowledge base is retired.',
  provider_unavailable: 'The embedding provider is disabled or unavailable.',
  provider_auth_failed: 'The embedding provider rejected the API key.',
  provider_model_not_found: 'The embedding model was not found at the provider.',
  provider_bad_request: 'The embedding provider rejected the request.',
  secret_unavailable: 'The provider key cannot be decrypted on this server.',
  capability_not_supported: 'The provider has no embeddings API.',
  embedding_dimensions_mismatch: 'The model returns vectors of another size than the profile.',
  embedding_invalid_response: 'The embedding provider returned invalid vectors.',
  egress_denied: 'The server’s network policy blocks the embedding provider.',
  attempts_exhausted: 'Temporary errors repeated too often.',
};

export function ingestionErrorMessage(code: string): string {
  return INGESTION_ERRORS[code] ?? `Processing failed (${code}).`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit] ?? 'GB'}`;
}

/** Where a search hit sits in its document: section, pages, sheet and rows (null parts are left out). */
export function hitLocation(hit: {
  section_path: string[];
  page_start: number | null;
  page_end: number | null;
  sheet_name: string | null;
  row_start: number | null;
  row_end: number | null;
}): string {
  const range = (start: number, end: number | null) =>
    end === null || end === start ? String(start) : `${String(start)}–${String(end)}`;
  const parts: string[] = [];
  if (hit.section_path.length > 0) parts.push(hit.section_path.join(' › '));
  if (hit.page_start !== null) parts.push(`p. ${range(hit.page_start, hit.page_end)}`);
  if (hit.sheet_name !== null) parts.push(`sheet ${hit.sheet_name}`);
  if (hit.row_start !== null) parts.push(`rows ${range(hit.row_start, hit.row_end)}`);
  return parts.join(' · ');
}
