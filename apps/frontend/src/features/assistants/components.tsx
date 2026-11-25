import { Badge } from '@chakra-ui/react';

import { unavailabilityLabel, type Assistant } from './assistantsApi';

/** The backend's verdict for the current user; the UI does not evaluate access itself. */
export function AvailabilityBadge({ assistant }: { assistant: Assistant }) {
  const { available, reason } = assistant.availability;
  if (available) return <Badge colorScheme="green">available</Badge>;
  return (
    <Badge colorScheme={reason === 'disabled' ? 'gray' : 'orange'} whiteSpace="normal">
      {reason !== null ? unavailabilityLabel(reason) : 'unavailable'}
    </Badge>
  );
}
