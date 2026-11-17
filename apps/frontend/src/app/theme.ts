import { extendTheme, type ThemeConfig } from '@chakra-ui/react';

const colorModeConfig: ThemeConfig = {
  initialColorMode: 'system',
  useSystemColorMode: true,
};

export const theme = extendTheme({
  config: colorModeConfig,
  colors: {
    brand: {
      50: '#f4f0fb',
      100: '#e2d7f5',
      500: '#6b46c1',
      600: '#553c9a',
      700: '#44337a',
    },
  },
});
