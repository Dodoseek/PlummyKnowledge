import {
  Alert,
  AlertDescription,
  AlertIcon,
  Badge,
  Box,
  Button,
  Heading,
  HStack,
  Stack,
  Text,
} from '@chakra-ui/react';
import { useNavigate, useParams, useSearchParams } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { useViewSourceQuery, type SourceView } from '../chat/chatApi';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function place(chunk: SourceView['chunk']): string | null {
  const pages =
    chunk.page_start === null
      ? null
      : chunk.page_end !== null && chunk.page_end !== chunk.page_start
        ? `pp. ${chunk.page_start}–${chunk.page_end}`
        : `p. ${chunk.page_start}`;
  const rows =
    chunk.row_start === null
      ? null
      : chunk.row_end !== null && chunk.row_end !== chunk.row_start
        ? `rows ${chunk.row_start}–${chunk.row_end}`
        : `row ${chunk.row_start}`;
  const parts = [
    chunk.section_path.length > 0 ? chunk.section_path.join(' › ') : null,
    pages,
    chunk.sheet_name !== null ? `sheet ${chunk.sheet_name}` : null,
    rows,
  ].filter((part): part is string => part !== null);
  return parts.length > 0 ? parts.join(' · ') : null;
}

/** Document text is untrusted: always rendered as plain text, never as HTML or Markdown. */
function Passage({ text, cited = false }: { text: string; cited?: boolean }) {
  return (
    <Box
      as="section"
      whiteSpace="pre-wrap"
      wordBreak="break-word"
      fontSize="sm"
      px={4}
      py={3}
      borderRadius="md"
      borderLeftWidth={cited ? '4px' : '0'}
      borderColor="brand.500"
      {...(cited ? { bg: 'yellow.50', _dark: { bg: 'gray.700' } } : { color: 'gray.500' })}
      data-testid={cited ? 'cited-passage' : 'context-passage'}
    >
      {text}
    </Box>
  );
}

/**
 * The cited passage with two neighbours on each side (API_CONTRACTS.md §2.11). The backend checks access to
 * the document on every request, so a citation seen earlier opens only while access lasts.
 */
export function SourceViewerPage() {
  const navigate = useNavigate();
  const { documentId = '', versionId = '' } = useParams();
  const [searchParams] = useSearchParams();
  const chunkId = searchParams.get('chunk') ?? '';
  const valid = [documentId, versionId, chunkId].every((id) => UUID.test(id));
  const view = useViewSourceQuery({ documentId, versionId, chunkId }, { skip: !valid });

  let body;
  if (!valid) {
    body = (
      <Alert status="warning" borderRadius="md">
        <AlertIcon />
        <AlertDescription>The link to the source is incomplete.</AlertDescription>
      </Alert>
    );
  } else if (view.isLoading) {
    body = <PageSpinner />;
  } else if (view.isError || view.data === undefined) {
    const error = toApiError(view.error);
    body =
      error.status === 404 ? (
        <Alert status="warning" borderRadius="md" data-testid="source-unavailable">
          <AlertIcon />
          <AlertDescription>
            The source is not available: the document was deleted or you no longer have access to it.
          </AlertDescription>
        </Alert>
      ) : (
        <ApiErrorAlert error={error} title="Cannot open the source" />
      );
  } else {
    const { document, version, chunk, context } = view.data;
    const where = place(chunk);
    body = (
      <Stack spacing={4}>
        <Stack spacing={1}>
          <Heading size="md" wordBreak="break-all">
            {document.filename}
          </Heading>
          <HStack spacing={2} flexWrap="wrap">
            <Text fontSize="sm" color="gray.500">
              Version {version.version_number}
            </Text>
            {!version.is_current && (
              <Badge colorScheme="orange" data-testid="outdated-version">
                Not the current version
              </Badge>
            )}
            {where !== null && (
              <Text fontSize="sm" color="gray.500">
                {where}
              </Text>
            )}
          </HStack>
        </Stack>
        <Stack spacing={2}>
          {context.before.map((text, index) => (
            <Passage key={`before-${index}`} text={text} />
          ))}
          <Passage text={chunk.content} cited />
          {context.after.map((text, index) => (
            <Passage key={`after-${index}`} text={text} />
          ))}
        </Stack>
      </Stack>
    );
  }

  return (
    <Stack spacing={4}>
      <Box>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            void navigate(-1);
          }}
        >
          ← Back
        </Button>
      </Box>
      {body}
    </Stack>
  );
}
