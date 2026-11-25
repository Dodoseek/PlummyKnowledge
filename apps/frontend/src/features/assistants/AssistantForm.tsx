import {
  Box,
  Button,
  Checkbox,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Heading,
  Input,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { useListEmbeddingProfilesQuery } from '../providers/providersApi';
import {
  modelLabel,
  useListAllKnowledgeBasesQuery,
  useListChatModelsQuery,
  type Assistant,
  type AssistantBody,
  type AssistantLimits,
  type AssistantRetrieval,
  type AssistantStatus,
} from './assistantsApi';

type NumberFields = Record<keyof AssistantRetrieval | keyof AssistantLimits, string>;

const RETRIEVAL_FIELDS: { name: keyof AssistantRetrieval; label: string; step?: string }[] = [
  { name: 'top_k', label: 'Chunks in the context' },
  { name: 'vector_top_k', label: 'Candidates by meaning' },
  { name: 'keyword_top_k', label: 'Candidates by keywords' },
  { name: 'rrf_k', label: 'Fusion constant (RRF k)' },
  { name: 'vector_weight', label: 'Weight of meaning search', step: '0.05' },
  { name: 'keyword_weight', label: 'Weight of keyword search', step: '0.05' },
];

const LIMIT_FIELDS: { name: keyof AssistantLimits; label: string }[] = [
  { name: 'max_context_tokens', label: 'Context tokens' },
  { name: 'max_history_messages', label: 'History messages' },
  { name: 'max_output_tokens', label: 'Answer tokens' },
];

function numbersOf(assistant: Assistant | undefined): NumberFields | null {
  if (assistant === undefined) return null;
  const values = { ...assistant.retrieval, ...assistant.limits };
  return Object.fromEntries(
    Object.entries(values).map(([key, value]) => [key, String(value)]),
  ) as NumberFields;
}

interface Props {
  /** Absent: a new assistant (retrieval and limits keep the server's defaults until it is edited). */
  assistant?: Assistant;
  submitLabel: string;
  isLoading: boolean;
  error: Parameters<typeof toApiError>[0];
  onSubmit: (body: AssistantBody) => void;
}

/** One form for creating and editing. Every rule is checked again by the backend (API_CONTRACTS.md §2.12). */
export function AssistantForm({ assistant, submitLabel, isLoading, error, onSubmit }: Props) {
  const models = useListChatModelsQuery();
  const profiles = useListEmbeddingProfilesQuery({});
  const knowledgeBases = useListAllKnowledgeBasesQuery();
  const [name, setName] = useState(assistant?.name ?? '');
  const [description, setDescription] = useState(assistant?.description ?? '');
  const [instructions, setInstructions] = useState(assistant?.instructions ?? '');
  const [modelId, setModelId] = useState(assistant?.model_config_id ?? '');
  const [profileId, setProfileId] = useState(assistant?.embedding_profile_id ?? '');
  const [kbIds, setKbIds] = useState<string[]>(assistant?.knowledge_base_ids ?? []);
  const [status, setStatus] = useState<AssistantStatus>(assistant?.status ?? 'active');
  const [numbers, setNumbers] = useState<NumberFields | null>(numbersOf(assistant));
  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);
  const otherError = apiError !== undefined && Object.keys(fields).length === 0 ? apiError : undefined;

  if (models.isLoading || profiles.isLoading || knowledgeBases.isLoading) return <PageSpinner />;
  const loadError = models.error ?? profiles.error ?? knowledgeBases.error;
  if (loadError !== undefined) {
    return <ApiErrorAlert error={toApiError(loadError)} title="Cannot load the choices" />;
  }

  // Disabled models and inactive profiles are offered only when the assistant already uses them.
  const modelOptions = (models.data ?? []).filter(
    (option) =>
      (option.model.enabled && option.provider.enabled) || option.model.id === assistant?.model_config_id,
  );
  const profileOptions = (profiles.data?.items ?? []).filter(
    (profile) => profile.status === 'active' || profile.id === assistant?.embedding_profile_id,
  );
  const selectedModel = modelId !== '' ? modelId : (modelOptions[0]?.model.id ?? '');
  const selectedProfile = profileId !== '' ? profileId : (profileOptions[0]?.id ?? '');
  // Every knowledge base of an assistant uses its embedding profile.
  const kbOptions = (knowledgeBases.data ?? []).filter((kb) => kb.embedding_profile_id === selectedProfile);
  const hidden = kbIds.filter((id) => !kbOptions.some((kb) => kb.id === id));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = description.trim();
    const body: AssistantBody = {
      name: name.trim(),
      description: trimmed === '' ? null : trimmed,
      instructions,
      model_config_id: selectedModel,
      embedding_profile_id: selectedProfile,
      knowledge_base_ids: kbIds.filter((id) => !hidden.includes(id)),
      status,
    };
    if (numbers !== null) {
      const value = (key: keyof NumberFields) => Number(numbers[key]);
      body.retrieval = Object.fromEntries(RETRIEVAL_FIELDS.map(({ name: key }) => [key, value(key)]));
      body.limits = Object.fromEntries(LIMIT_FIELDS.map(({ name: key }) => [key, value(key)]));
    }
    onSubmit(body);
  };

  if (modelOptions.length === 0 || profileOptions.length === 0) {
    return (
      <Text color="gray.500">
        An assistant needs an enabled chat model and an active embedding profile. Add them under AI providers
        first.
      </Text>
    );
  }

  const numberInput = (key: keyof NumberFields, label: string, step = '1') => (
    <FormControl key={key} isInvalid={fields[key] !== undefined}>
      <FormLabel fontSize="sm">{label}</FormLabel>
      <Input
        type="number"
        step={step}
        value={numbers?.[key] ?? ''}
        onChange={(event) => {
          if (numbers !== null) setNumbers({ ...numbers, [key]: event.target.value });
        }}
      />
      <FormErrorMessage>{fields[key]}</FormErrorMessage>
    </FormControl>
  );

  return (
    <form onSubmit={submit} noValidate>
      <Stack spacing={4}>
        <SimpleGrid columns={{ base: 1, md: 2 }} spacing={4}>
          <FormControl isRequired isInvalid={fields.name !== undefined}>
            <FormLabel>Name</FormLabel>
            <Input
              value={name}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
            <FormErrorMessage>{fields.name}</FormErrorMessage>
          </FormControl>
          <FormControl isInvalid={fields.status !== undefined}>
            <FormLabel>Status</FormLabel>
            <Select
              value={status}
              onChange={(event) => {
                setStatus(event.target.value as AssistantStatus);
              }}
            >
              <option value="active">active</option>
              <option value="disabled">disabled — nobody can use it</option>
            </Select>
          </FormControl>
        </SimpleGrid>
        <FormControl isInvalid={fields.description !== undefined}>
          <FormLabel>Description</FormLabel>
          <Input
            value={description}
            onChange={(event) => {
              setDescription(event.target.value);
            }}
          />
          <FormErrorMessage>{fields.description}</FormErrorMessage>
        </FormControl>
        <FormControl isInvalid={fields.instructions !== undefined}>
          <FormLabel>Instructions</FormLabel>
          <Textarea
            rows={6}
            value={instructions}
            onChange={(event) => {
              setInstructions(event.target.value);
            }}
          />
          <FormErrorMessage>{fields.instructions}</FormErrorMessage>
          <FormHelperText>
            Sent to the model with every question. Never put keys or passwords here.
          </FormHelperText>
        </FormControl>
        <SimpleGrid columns={{ base: 1, md: 2 }} spacing={4}>
          <FormControl isRequired isInvalid={fields.model_config_id !== undefined}>
            <FormLabel>Chat model</FormLabel>
            <Select
              value={selectedModel}
              onChange={(event) => {
                setModelId(event.target.value);
              }}
            >
              {modelOptions.map((option) => (
                <option key={option.model.id} value={option.model.id}>
                  {modelLabel(option)}
                </option>
              ))}
            </Select>
            <FormErrorMessage>{fields.model_config_id}</FormErrorMessage>
          </FormControl>
          <FormControl isRequired isInvalid={fields.embedding_profile_id !== undefined}>
            <FormLabel>Embedding profile</FormLabel>
            <Select
              value={selectedProfile}
              onChange={(event) => {
                setProfileId(event.target.value);
              }}
            >
              {profileOptions.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name} ({profile.model_id})
                  {profile.status !== 'active' ? ` — ${profile.status}` : ''}
                </option>
              ))}
            </Select>
            <FormErrorMessage>{fields.embedding_profile_id}</FormErrorMessage>
          </FormControl>
        </SimpleGrid>
        {/* A group of checkboxes: a fieldset, not one form control (each checkbox keeps its own id). */}
        <Box as="fieldset">
          <Text as="legend" fontWeight="medium" mb={2}>
            Knowledge bases
          </Text>
          {kbOptions.length === 0 ? (
            <Text fontSize="sm" color="gray.500">
              No knowledge base you can read uses this embedding profile.
            </Text>
          ) : (
            <Stack spacing={1} data-testid="assistant-knowledge-bases">
              {kbOptions.map((kb) => (
                <Checkbox
                  key={kb.id}
                  isChecked={kbIds.includes(kb.id)}
                  onChange={(event) => {
                    setKbIds(event.target.checked ? [...kbIds, kb.id] : kbIds.filter((id) => id !== kb.id));
                  }}
                >
                  {kb.name}
                </Checkbox>
              ))}
            </Stack>
          )}
          {fields.knowledge_base_ids !== undefined && (
            <Text fontSize="sm" color="red.500" mt={2} role="alert">
              {fields.knowledge_base_ids}
            </Text>
          )}
          <Text fontSize="sm" color="gray.500" mt={2}>
            Only users with access to every chosen knowledge base can use the assistant.
            {hidden.length > 0 && ' Knowledge bases of another profile are removed on save.'}
          </Text>
        </Box>
        {numbers !== null && (
          <>
            <Heading size="sm">Retrieval</Heading>
            <SimpleGrid columns={{ base: 1, md: 3 }} spacing={4}>
              {RETRIEVAL_FIELDS.map(({ name: key, label, step }) => numberInput(key, label, step))}
            </SimpleGrid>
            <Heading size="sm">Limits</Heading>
            <SimpleGrid columns={{ base: 1, md: 3 }} spacing={4}>
              {LIMIT_FIELDS.map(({ name: key, label }) => numberInput(key, label))}
            </SimpleGrid>
          </>
        )}
        {otherError !== undefined && (
          <ApiErrorAlert error={{ ...otherError, message: messageFor(otherError) }} title="Not saved" />
        )}
        <Button
          type="submit"
          colorScheme="brand"
          alignSelf="flex-start"
          isLoading={isLoading}
          isDisabled={name.trim() === ''}
        >
          {submitLabel}
        </Button>
      </Stack>
    </form>
  );
}
