import {
  Alert,
  AlertDescription,
  AlertIcon,
  Button,
  Card,
  CardBody,
  Code,
  FormControl,
  FormErrorMessage,
  FormHelperText,
  FormLabel,
  Heading,
  Input,
  Link,
  Stack,
  Text,
} from '@chakra-ui/react';
import { useState, type FormEvent } from 'react';
import { Navigate, Link as RouterLink, useNavigate } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { fieldErrorsByName, messageFor } from '../../shared/api/messages';
import { ErrorWithRetry } from '../../shared/components/ErrorWithRetry';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { useGetSetupStatusQuery, useInitializeSetupMutation } from './setupApi';

export const MIN_PASSWORD_LENGTH = 12;
export const MAX_PASSWORD_LENGTH = 128;

interface FormValues {
  bootstrapToken: string;
  email: string;
  displayName: string;
  password: string;
  confirmPassword: string;
}

const EMPTY: FormValues = {
  bootstrapToken: '',
  email: '',
  displayName: '',
  password: '',
  confirmPassword: '',
};

/** Client-side hints only; the backend enforces every rule (SECURITY_MODEL.md §4.1). */
function validate(values: FormValues): Partial<Record<keyof FormValues, string>> {
  const errors: Partial<Record<keyof FormValues, string>> = {};
  if (values.bootstrapToken.trim() === '') errors.bootstrapToken = 'Enter the setup token from the server.';
  if (values.email.trim() === '') errors.email = 'Enter an email.';
  if (values.displayName.trim() === '') errors.displayName = 'Enter a name.';
  if (values.password.length < MIN_PASSWORD_LENGTH) {
    errors.password = `Use at least ${MIN_PASSWORD_LENGTH} characters.`;
  } else if (values.password.length > MAX_PASSWORD_LENGTH) {
    errors.password = `Use at most ${MAX_PASSWORD_LENGTH} characters.`;
  }
  if (values.confirmPassword !== values.password) errors.confirmPassword = 'Passwords do not match.';
  return errors;
}

export function SetupPage() {
  const navigate = useNavigate();
  const status = useGetSetupStatusQuery();
  const [initialize, { isLoading, error }] = useInitializeSetupMutation();
  const [values, setValues] = useState<FormValues>(EMPTY);
  const [submitted, setSubmitted] = useState(false);

  if (status.isLoading) {
    return <PageSpinner />;
  }
  if (status.isError || status.data === undefined) {
    return (
      <ErrorWithRetry
        error={toApiError(status.error)}
        title="Cannot load the setup state"
        onRetry={() => void status.refetch()}
      />
    );
  }
  if (status.data.status === 'initialized') {
    return <Navigate to="/login" replace />;
  }
  if (!status.data.web_setup_enabled) {
    return (
      <Stack maxW="lg" mx="auto" spacing={4}>
        <Heading size="lg">Initial setup</Heading>
        <Text>
          Web setup is disabled on this server. The server operator creates the first administrator with{' '}
          <Code>python -m plummy.cli admin create</Code>.
        </Text>
      </Stack>
    );
  }

  const clientErrors = submitted ? validate(values) : {};
  const apiError = error !== undefined ? toApiError(error) : undefined;
  const serverErrors = fieldErrorsByName(apiError);
  const fieldError = (name: keyof FormValues, serverName?: string): string | undefined =>
    clientErrors[name] ?? (serverName !== undefined ? serverErrors[serverName] : undefined);
  const generalError = apiError !== undefined && apiError.fieldErrors === undefined ? apiError : undefined;

  const set = (name: keyof FormValues) => (event: { target: { value: string } }) => {
    setValues((current) => ({ ...current, [name]: event.target.value }));
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (Object.keys(validate(values)).length > 0) return;
    try {
      await initialize({
        bootstrap_token: values.bootstrapToken.trim(),
        admin: {
          email: values.email.trim(),
          display_name: values.displayName.trim(),
          password: values.password,
        },
      }).unwrap();
      void navigate('/', { replace: true });
    } catch {
      // Errors are rendered from the mutation state.
    }
  };

  return (
    <Stack maxW="lg" mx="auto" spacing={6}>
      <Stack spacing={2}>
        <Heading size="lg">Initial setup</Heading>
        <Text color="gray.500">
          Create the first administrator. The one-time setup token is on the server in{' '}
          <Code>BOOTSTRAP_DIR/setup-token</Code> (see docs/DEPLOYMENT.md).
        </Text>
      </Stack>
      <Card>
        <CardBody>
          <form onSubmit={(event) => void onSubmit(event)} noValidate>
            <Stack spacing={4}>
              {generalError !== undefined && (
                <Alert status="error" borderRadius="md" role="alert">
                  <AlertIcon />
                  <AlertDescription>
                    {messageFor(generalError)}{' '}
                    {generalError.code === 'setup_already_completed' && (
                      <Link as={RouterLink} to="/login" color="brand.500">
                        Go to sign in
                      </Link>
                    )}
                  </AlertDescription>
                </Alert>
              )}
              <FormControl
                isRequired
                isInvalid={fieldError('bootstrapToken', 'bootstrap_token') !== undefined}
              >
                <FormLabel>Setup token</FormLabel>
                <Input
                  type="password"
                  name="bootstrap_token"
                  autoComplete="off"
                  value={values.bootstrapToken}
                  onChange={set('bootstrapToken')}
                />
                <FormErrorMessage>{fieldError('bootstrapToken', 'bootstrap_token')}</FormErrorMessage>
              </FormControl>
              <FormControl isRequired isInvalid={fieldError('email', 'email') !== undefined}>
                <FormLabel>Administrator email</FormLabel>
                <Input
                  type="email"
                  name="email"
                  autoComplete="username"
                  value={values.email}
                  onChange={set('email')}
                />
                <FormErrorMessage>{fieldError('email', 'email')}</FormErrorMessage>
              </FormControl>
              <FormControl isRequired isInvalid={fieldError('displayName', 'display_name') !== undefined}>
                <FormLabel>Name</FormLabel>
                <Input name="display_name" value={values.displayName} onChange={set('displayName')} />
                <FormErrorMessage>{fieldError('displayName', 'display_name')}</FormErrorMessage>
              </FormControl>
              <FormControl isRequired isInvalid={fieldError('password', 'password') !== undefined}>
                <FormLabel>Password</FormLabel>
                <Input
                  type="password"
                  name="password"
                  autoComplete="new-password"
                  value={values.password}
                  onChange={set('password')}
                />
                <FormHelperText>
                  {MIN_PASSWORD_LENGTH}–{MAX_PASSWORD_LENGTH} characters, not your email.
                </FormHelperText>
                <FormErrorMessage>{fieldError('password', 'password')}</FormErrorMessage>
              </FormControl>
              <FormControl isRequired isInvalid={fieldError('confirmPassword') !== undefined}>
                <FormLabel>Repeat password</FormLabel>
                <Input
                  type="password"
                  name="confirm_password"
                  autoComplete="new-password"
                  value={values.confirmPassword}
                  onChange={set('confirmPassword')}
                />
                <FormErrorMessage>{fieldError('confirmPassword')}</FormErrorMessage>
              </FormControl>
              <Button type="submit" colorScheme="brand" isLoading={isLoading}>
                Create administrator
              </Button>
            </Stack>
          </form>
        </CardBody>
      </Card>
    </Stack>
  );
}
