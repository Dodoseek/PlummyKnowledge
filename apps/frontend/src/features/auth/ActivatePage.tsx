import {
  Alert,
  AlertDescription,
  AlertIcon,
  Button,
  Card,
  CardBody,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Heading,
  Input,
  Stack,
  Text,
} from '@chakra-ui/react';
import { useEffect, useState, type FormEvent } from 'react';
import { Link as RouterLink, useLocation, useNavigate } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { useActivateMutation } from './authApi';

const TOKEN_PARAM = 'token';

/** The token is in the URL fragment (ADR-015): browsers never send it to servers or in `Referer`. */
function tokenFromHash(hash: string): string | null {
  const value = new URLSearchParams(hash.replace(/^#/, '')).get(TOKEN_PARAM);
  return value !== null && value.trim() !== '' ? value.trim() : null;
}

/**
 * Account activation by a one-time link. The token lives only in this component's state: it is removed from
 * the address bar right away and never stored in Redux, localStorage or sessionStorage.
 */
export function ActivatePage() {
  const location = useLocation();
  const navigate = useNavigate();
  const [token] = useState(() => tokenFromHash(location.hash));
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [activate, { isLoading, error }] = useActivateMutation();

  useEffect(() => {
    if (location.hash !== '') {
      void navigate({ pathname: location.pathname, search: location.search }, { replace: true });
    }
  }, [location.hash, location.pathname, location.search, navigate]);

  if (token === null) {
    return (
      <Stack maxW="md" mx="auto" spacing={4}>
        <Heading size="lg">Activate your account</Heading>
        <Alert status="error" borderRadius="md" role="alert">
          <AlertIcon />
          <AlertDescription>
            This page needs the full activation link. Open the link you received from your administrator.
          </AlertDescription>
        </Alert>
        <Text>
          Already activated? <RouterLink to="/login">Sign in</RouterLink>.
        </Text>
      </Stack>
    );
  }

  const apiError = error === undefined ? undefined : toApiError(error);
  const fields = fieldErrorsByName(apiError);
  const mismatch = submitted && password !== confirmation;
  const missing = submitted && password === '';

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (password === '' || password !== confirmation) return;
    try {
      await activate({ token, password }).unwrap();
      void navigate('/', { replace: true });
    } catch {
      // Shown below; the link stays usable after a password policy error.
    }
  };

  return (
    <Stack maxW="md" mx="auto" spacing={6}>
      <Heading size="lg">Activate your account</Heading>
      <Card>
        <CardBody>
          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            <Stack spacing={4}>
              {apiError !== undefined && fields.password === undefined && (
                <Alert status="error" borderRadius="md" role="alert">
                  <AlertIcon />
                  <AlertDescription>{messageFor(apiError)}</AlertDescription>
                </Alert>
              )}
              <FormControl isRequired isInvalid={missing || fields.password !== undefined}>
                <FormLabel>New password</FormLabel>
                <Input
                  type="password"
                  name="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(event) => {
                    setPassword(event.target.value);
                  }}
                />
                <FormErrorMessage>{fields.password ?? 'Choose a password.'}</FormErrorMessage>
                <FormHelperText>At least 12 characters. A long passphrase is best.</FormHelperText>
              </FormControl>
              <FormControl isRequired isInvalid={mismatch}>
                <FormLabel>Repeat password</FormLabel>
                <Input
                  type="password"
                  name="confirmation"
                  autoComplete="new-password"
                  value={confirmation}
                  onChange={(event) => {
                    setConfirmation(event.target.value);
                  }}
                />
                <FormErrorMessage>The passwords do not match.</FormErrorMessage>
              </FormControl>
              <Button type="submit" colorScheme="brand" isLoading={isLoading}>
                Activate and sign in
              </Button>
            </Stack>
          </form>
        </CardBody>
      </Card>
    </Stack>
  );
}
