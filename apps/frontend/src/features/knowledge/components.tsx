import { Badge, HStack, Text } from '@chakra-ui/react';

import type { AccessLevel, DocumentStatus, KnowledgeBase } from './knowledgeApi';

const STATUS: Record<DocumentStatus, { label: string; color: string }> = {
  pending: { label: 'queued', color: 'gray' },
  processing: { label: 'processing', color: 'blue' },
  indexed: { label: 'indexed', color: 'green' },
  failed: { label: 'failed', color: 'red' },
  skipped: { label: 'skipped', color: 'yellow' },
};

export function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  const { label, color } = STATUS[status];
  return <Badge colorScheme={color}>{label}</Badge>;
}

export function AccessBadge({ level }: { level: AccessLevel }) {
  return <Badge colorScheme={level === 'manage' ? 'purple' : 'gray'}>{level}</Badge>;
}

export function IndexingSummary({ indexing }: { indexing: KnowledgeBase['indexing'] }) {
  return (
    <HStack spacing={3} fontSize="sm">
      <Text>{indexing.documents_total} total</Text>
      <Text color="green.600">{indexing.indexed} indexed</Text>
      {indexing.processing > 0 && <Text color="blue.600">{indexing.processing} in progress</Text>}
      {indexing.failed > 0 && <Text color="red.600">{indexing.failed} failed</Text>}
    </HStack>
  );
}
