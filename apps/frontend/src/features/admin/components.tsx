import {
  Alert,
  AlertDescription,
  AlertIcon,
  AlertTitle,
  Badge,
  Box,
  Button,
  Checkbox,
  Code,
  HStack,
  Stack,
  Text,
  useClipboard,
} from '@chakra-ui/react';
import { useState } from 'react';

import { absoluteLink, type ActivationLink, type Role, type UserStatus } from './adminApi';
import type { CursorPager } from './useCursorPager';

const STATUS_COLORS: Record<UserStatus, string> = { active: 'green', invited: 'blue', disabled: 'gray' };

export function StatusBadge({ status }: { status: UserStatus }) {
  return <Badge colorScheme={STATUS_COLORS[status]}>{status}</Badge>;
}

/** The activation link is shown once: it is not stored anywhere in the browser. */
export function ActivationLinkNotice({ link }: { link: ActivationLink }) {
  const url = absoluteLink(link.url);
  const { onCopy, hasCopied } = useClipboard(url);
  return (
    <Alert status="info" borderRadius="md" alignItems="flex-start" data-testid="activation-link">
      <AlertIcon />
      <Box flex="1" minW={0}>
        <AlertTitle>Activation link</AlertTitle>
        <AlertDescription>
          <Stack spacing={2}>
            <Text>
              Send this link to the user over a trusted channel. It works once and expires{' '}
              {new Date(link.expires_at).toLocaleString()}. It will not be shown again.
            </Text>
            <Code p={2} wordBreak="break-all" whiteSpace="normal">
              {url}
            </Code>
            <Button size="sm" alignSelf="flex-start" onClick={onCopy}>
              {hasCopied ? 'Copied' : 'Copy link'}
            </Button>
          </Stack>
        </AlertDescription>
      </Box>
    </Alert>
  );
}

interface RoleCheckboxesProps {
  roles: Role[];
  value: string[];
  onChange: (value: string[]) => void;
  exclude?: string[];
  isDisabled?: boolean;
}

export function RoleCheckboxes({
  roles,
  value,
  onChange,
  exclude = [],
  isDisabled = false,
}: RoleCheckboxesProps) {
  return (
    <Stack spacing={2}>
      {roles
        .filter((role) => !exclude.includes(role.key))
        .map((role) => (
          <Checkbox
            key={role.key}
            isChecked={value.includes(role.key)}
            isDisabled={isDisabled}
            onChange={(event) => {
              onChange(event.target.checked ? [...value, role.key] : value.filter((key) => key !== role.key));
            }}
          >
            <Text as="span" fontWeight="medium">
              {role.name}
            </Text>{' '}
            <Text as="span" color="gray.500" fontSize="sm">
              {role.description}
            </Text>
          </Checkbox>
        ))}
    </Stack>
  );
}

/** A destructive action that needs a second click to confirm. */
export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  isLoading = false,
}: {
  label: string;
  confirmLabel: string;
  onConfirm: () => void;
  isLoading?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  if (!armed) {
    return (
      <Button
        size="sm"
        colorScheme="red"
        variant="outline"
        onClick={() => {
          setArmed(true);
        }}
      >
        {label}
      </Button>
    );
  }
  return (
    <HStack>
      <Button size="sm" colorScheme="red" onClick={onConfirm} isLoading={isLoading}>
        {confirmLabel}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          setArmed(false);
        }}
      >
        Cancel
      </Button>
    </HStack>
  );
}

export function Pager({ pager, nextCursor }: { pager: CursorPager; nextCursor: string | null | undefined }) {
  return (
    <HStack justify="flex-end" spacing={3}>
      <Text fontSize="sm" color="gray.500">
        Page {pager.page}
      </Text>
      <Button size="sm" onClick={pager.previous} isDisabled={pager.page === 1}>
        Previous
      </Button>
      <Button
        size="sm"
        onClick={() => {
          if (nextCursor) pager.next(nextCursor);
        }}
        isDisabled={nextCursor === null || nextCursor === undefined}
      >
        Next
      </Button>
    </HStack>
  );
}
