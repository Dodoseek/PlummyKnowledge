import {
  Alert,
  AlertDescription,
  AlertIcon,
  Badge,
  Box,
  Button,
  Card,
  CardBody,
  CardHeader,
  FormControl,
  FormErrorMessage,
  FormLabel,
  Heading,
  HStack,
  Input,
  Select,
  Stack,
  Text,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { hitLocation } from './format';
import { useSearchKnowledgeBaseMutation, type SearchHit, type SearchMode } from './knowledgeApi';

const MODES: { value: SearchMode; label: string }[] = [
  { value: 'hybrid', label: 'Hybrid' },
  { value: 'keyword', label: 'Keywords' },
  { value: 'vector', label: 'Meaning' },
];

function Hit({ hit }: { hit: SearchHit }) {
  const location = hitLocation(hit);
  return (
    <Box borderWidth="1px" borderRadius="md" p={3} data-testid="search-hit">
      <HStack justify="space-between" align="flex-start" spacing={3}>
        <Stack spacing={0} minW={0}>
          <Text fontWeight="medium" wordBreak="break-word">
            {hit.filename}
          </Text>
          {location !== '' && (
            <Text fontSize="sm" color="gray.500" wordBreak="break-word">
              {location}
            </Text>
          )}
        </Stack>
        <HStack spacing={1} flexShrink={0}>
          {hit.keyword_rank !== null && <Badge colorScheme="purple">keywords #{hit.keyword_rank}</Badge>}
          {hit.vector_rank !== null && <Badge colorScheme="blue">meaning #{hit.vector_rank}</Badge>}
        </HStack>
      </HStack>
      {/* Document text is untrusted: plain text only, never HTML or Markdown. */}
      <Text mt={2} fontSize="sm" whiteSpace="pre-wrap" wordBreak="break-word" noOfLines={6}>
        {hit.content}
      </Text>
    </Box>
  );
}

export function SearchCard({ kbId }: { kbId: string }) {
  const [search, state] = useSearchKnowledgeBaseMutation();
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<SearchMode>('hybrid');
  const apiError = state.error === undefined ? undefined : toApiError(state.error);
  const queryError = fieldErrorsByName(apiError).query;

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (query.trim() === '') return;
    void search({ kbId, query, mode });
  };

  return (
    <Card>
      <CardHeader pb={0}>
        <Heading size="md">Search</Heading>
      </CardHeader>
      <CardBody>
        <Stack spacing={4}>
          <form onSubmit={onSubmit} noValidate role="search">
            <HStack align="flex-end" spacing={3} flexWrap="wrap">
              <FormControl flex="1" minW="16rem" isInvalid={queryError !== undefined}>
                <FormLabel>Query</FormLabel>
                <Input
                  value={query}
                  maxLength={1000}
                  placeholder="What do the documents say about…"
                  onChange={(event) => {
                    setQuery(event.target.value);
                  }}
                />
                {queryError !== undefined && <FormErrorMessage>{queryError}</FormErrorMessage>}
              </FormControl>
              <FormControl w="auto">
                <FormLabel>Mode</FormLabel>
                <Select
                  value={mode}
                  onChange={(event) => {
                    setMode(event.target.value as SearchMode);
                  }}
                >
                  {MODES.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </Select>
              </FormControl>
              <Button
                type="submit"
                colorScheme="brand"
                isLoading={state.isLoading}
                isDisabled={query.trim() === ''}
              >
                Search
              </Button>
            </HStack>
          </form>
          {apiError !== undefined && queryError === undefined && (
            <ApiErrorAlert error={{ ...apiError, message: messageFor(apiError) }} title="Search failed" />
          )}
          {state.data !== undefined && (
            <Stack spacing={3}>
              {state.data.keyword_query_empty && state.data.mode !== 'vector' && (
                <Alert status="info" borderRadius="md">
                  <AlertIcon />
                  <AlertDescription>
                    The query has only common words, so no keyword matches were looked for.
                  </AlertDescription>
                </Alert>
              )}
              {state.data.items.length === 0 ? (
                <Text color="gray.500">Nothing found.</Text>
              ) : (
                state.data.items.map((hit) => <Hit key={hit.chunk_id} hit={hit} />)
              )}
            </Stack>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
}
