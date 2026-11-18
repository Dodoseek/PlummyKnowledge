import { Center, Spinner } from '@chakra-ui/react';

export function PageSpinner({ label = 'Loading' }: { label?: string }) {
  return (
    <Center py={16}>
      <Spinner size="lg" color="brand.500" label={label} data-testid="page-spinner" />
    </Center>
  );
}
