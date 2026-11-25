import type { Citation } from './chatApi';

export const IDS = {
  document: '0190a000-0000-7000-8000-000000000101',
  version: '0190a000-0000-7000-8000-000000000102',
  chunk: '0190a000-0000-7000-8000-000000000103',
};

/** A validated citation as the backend sends it (API_CONTRACTS.md §3.2). */
export const CITATION: Citation = {
  citation_id: 'c1',
  document_id: IDS.document,
  document_version_id: IDS.version,
  chunk_id: IDS.chunk,
  filename: 'handbook.pdf',
  page: 4,
  section: 'Vacation',
  sheet: null,
  row: null,
  snippet: 'Vacation requests are filed two weeks ahead.',
  viewer_route: `/documents/${IDS.document}/versions/${IDS.version}/chunks/${IDS.chunk}/view`,
};
