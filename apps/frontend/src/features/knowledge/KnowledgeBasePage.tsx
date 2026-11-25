import {
  Alert,
  AlertDescription,
  AlertIcon,
  Button,
  Card,
  CardBody,
  CardHeader,
  FormControl,
  FormHelperText,
  FormLabel,
  Heading,
  HStack,
  Input,
  Link,
  List,
  ListItem,
  Stack,
  Table,
  TableContainer,
  Tbody,
  Td,
  Text,
  Th,
  Thead,
  Tr,
} from '@chakra-ui/react';
import type { SerializedError } from '@reduxjs/toolkit';
import type { FetchBaseQueryError } from '@reduxjs/toolkit/query';
import { useRef, useState, type FormEvent } from 'react';
import { Link as RouterLink, useParams } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { ConfirmButton, Pager } from '../admin/components';
import { useCursorPager, type CursorPager } from '../admin/useCursorPager';
import { AccessBadge, DocumentStatusBadge, IndexingSummary } from './components';
import { formatBytes, ingestionErrorMessage } from './format';
import { SearchCard } from './SearchCard';
import {
  isInProgress,
  useDeleteDocumentMutation,
  useGetKnowledgeBaseQuery,
  useListDocumentsQuery,
  useReindexDocumentMutation,
  useUploadDocumentMutation,
  type KbDocument,
} from './knowledgeApi';
import type { Page } from '../admin/adminApi';

/** While documents are in the pipeline the list and the counters refresh on their own. */
export const POLL_INTERVAL_MS = 3000;
const ACCEPT = '.pdf,.docx,.xlsx,.txt,.csv,.md,.markdown';

interface UploadFailure {
  filename: string;
  message: string;
}

function UploadCard({ kbId }: { kbId: string }) {
  const [uploadDocument] = useUploadDocumentMutation();
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [uploaded, setUploaded] = useState(0);
  const [failures, setFailures] = useState<UploadFailure[]>([]);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setUploaded(0);
    setFailures([]);
    // One request per file, one after another: the per-user rate limit and the size check apply per file.
    for (const file of files) {
      try {
        await uploadDocument({ kbId, file }).unwrap();
        setUploaded((count) => count + 1);
      } catch (error) {
        const apiError = toApiError(error as FetchBaseQueryError | SerializedError);
        // A field error (empty file) carries the precise reason; other errors map by code.
        const message = apiError.fieldErrors?.[0]?.message ?? messageFor(apiError);
        setFailures((list) => [...list, { filename: file.name, message }]);
      }
    }
    setBusy(false);
    setFiles([]);
    if (input.current !== null) input.current.value = '';
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Upload documents</Heading>
      </CardHeader>
      <CardBody>
        <form onSubmit={(event) => void onSubmit(event)} noValidate>
          <Stack spacing={4}>
            <FormControl>
              <FormLabel>Files</FormLabel>
              <Input
                ref={input}
                type="file"
                multiple
                accept={ACCEPT}
                p={1}
                onChange={(event) => {
                  setFiles(Array.from(event.target.files ?? []));
                }}
              />
              <FormHelperText>
                PDF, DOCX, XLSX, TXT, CSV or Markdown. The type is checked by the server from the content.
              </FormHelperText>
            </FormControl>
            {uploaded > 0 && !busy && (
              <Alert status="success" borderRadius="md">
                <AlertIcon />
                <AlertDescription>
                  {uploaded === 1 ? '1 file queued' : `${String(uploaded)} files queued`} for processing.
                </AlertDescription>
              </Alert>
            )}
            {failures.length > 0 && (
              <Alert status="error" borderRadius="md" alignItems="flex-start" data-testid="upload-failures">
                <AlertIcon />
                <List spacing={1}>
                  {failures.map((failure, index) => (
                    <ListItem key={`${failure.filename}-${String(index)}`}>
                      <Text as="span" fontWeight="medium">
                        {failure.filename}
                      </Text>
                      : {failure.message}
                    </ListItem>
                  ))}
                </List>
              </Alert>
            )}
            <Button
              type="submit"
              colorScheme="brand"
              alignSelf="flex-start"
              isLoading={busy}
              isDisabled={files.length === 0}
            >
              Upload
            </Button>
          </Stack>
        </form>
      </CardBody>
    </Card>
  );
}

function DocumentRow({
  document,
  kbId,
  canManage,
}: {
  document: KbDocument;
  kbId: string;
  canManage: boolean;
}) {
  const [deleteDocument, deleteState] = useDeleteDocumentMutation();
  const [reindexDocument, reindexState] = useReindexDocumentMutation();
  const version = document.latest_version;
  const errorCode = document.processing_status === 'failed' ? version?.error_code : null;
  const actionError = deleteState.error ?? reindexState.error;
  const apiError = actionError === undefined ? undefined : toApiError(actionError);

  return (
    <>
      <Tr>
        <Td whiteSpace="normal" wordBreak="break-word">
          {document.filename}
        </Td>
        <Td>
          <DocumentStatusBadge status={document.processing_status} />
          {errorCode != null && (
            <Text fontSize="sm" color="red.600" whiteSpace="normal">
              {ingestionErrorMessage(errorCode)}
            </Text>
          )}
        </Td>
        <Td isNumeric>{version !== null ? formatBytes(version.size_bytes) : '—'}</Td>
        <Td>{new Date(document.created_at).toLocaleString()}</Td>
        {canManage && (
          <Td>
            <HStack>
              {!isInProgress(document.processing_status) && (
                <Button
                  size="sm"
                  variant="outline"
                  isLoading={reindexState.isLoading}
                  onClick={() => void reindexDocument({ kbId, id: document.id })}
                >
                  Reindex
                </Button>
              )}
              <ConfirmButton
                label="Delete"
                confirmLabel="Delete document"
                isLoading={deleteState.isLoading}
                onConfirm={() => void deleteDocument({ kbId, id: document.id })}
              />
            </HStack>
          </Td>
        )}
      </Tr>
      {apiError !== undefined && (
        <Tr>
          <Td colSpan={canManage ? 5 : 4} whiteSpace="normal">
            <ApiErrorAlert error={{ ...apiError, message: messageFor(apiError) }} title="Not done" />
          </Td>
        </Tr>
      )}
    </>
  );
}

function DocumentsCard({
  kbId,
  canManage,
  pager,
  documents,
}: {
  kbId: string;
  canManage: boolean;
  pager: CursorPager;
  documents: {
    data?: Page<KbDocument> | undefined;
    isLoading: boolean;
    isError: boolean;
    error?: FetchBaseQueryError | SerializedError | undefined;
  };
}) {
  const items = documents.data?.items ?? [];

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Documents</Heading>
      </CardHeader>
      <CardBody>
        {documents.isLoading && <PageSpinner />}
        {documents.isError && (
          <ApiErrorAlert error={toApiError(documents.error)} title="Cannot load documents" />
        )}
        {documents.data !== undefined && (
          <Stack spacing={4}>
            {items.length === 0 ? (
              <Text color="gray.500">No documents yet.</Text>
            ) : (
              <TableContainer>
                <Table size="sm">
                  <Thead>
                    <Tr>
                      <Th>File</Th>
                      <Th>Status</Th>
                      <Th isNumeric>Size</Th>
                      <Th>Uploaded</Th>
                      {canManage && <Th />}
                    </Tr>
                  </Thead>
                  <Tbody>
                    {items.map((document) => (
                      <DocumentRow key={document.id} document={document} kbId={kbId} canManage={canManage} />
                    ))}
                  </Tbody>
                </Table>
              </TableContainer>
            )}
            <Pager pager={pager} nextCursor={documents.data.next_cursor} />
          </Stack>
        )}
      </CardBody>
    </Card>
  );
}

export function KnowledgeBasePage() {
  const { kbId = '' } = useParams();
  const pager = useCursorPager();
  // The previous result decides whether to keep polling: stop once nothing is queued or processing.
  const [polling, setPolling] = useState(false);
  const pollingOptions = { pollingInterval: polling ? POLL_INTERVAL_MS : 0, skipPollingIfUnfocused: true };
  const kb = useGetKnowledgeBaseQuery(kbId, pollingOptions);
  const documents = useListDocumentsQuery(
    { kbId, ...(pager.cursor !== undefined ? { cursor: pager.cursor } : {}) },
    { ...pollingOptions, skip: kb.data === undefined },
  );
  const inProgress = (documents.data?.items ?? []).some((document) =>
    isInProgress(document.processing_status),
  );
  if (inProgress !== polling) setPolling(inProgress);

  if (kb.isLoading) return <PageSpinner />;
  if (kb.isError || kb.data === undefined) {
    return (
      <Stack spacing={4}>
        <Link as={RouterLink} to="/knowledge-bases" color="brand.500" fontSize="sm">
          ← Knowledge bases
        </Link>
        <ApiErrorAlert error={toApiError(kb.error)} title="Knowledge base not available" />
      </Stack>
    );
  }
  // Only for showing controls: the backend checks access to the knowledge base on every request.
  const canManage = kb.data.my_access_level === 'manage';

  return (
    <Stack spacing={6}>
      <Link as={RouterLink} to="/knowledge-bases" color="brand.500" fontSize="sm">
        ← Knowledge bases
      </Link>
      <Stack spacing={2}>
        <HStack spacing={3}>
          <Heading size="lg">{kb.data.name}</Heading>
          <AccessBadge level={kb.data.my_access_level} />
        </HStack>
        {kb.data.description !== null && <Text color="gray.500">{kb.data.description}</Text>}
        <IndexingSummary indexing={kb.data.indexing} />
      </Stack>
      <SearchCard kbId={kbId} />
      {canManage && <UploadCard kbId={kbId} />}
      <DocumentsCard kbId={kbId} canManage={canManage} pager={pager} documents={documents} />
    </Stack>
  );
}
