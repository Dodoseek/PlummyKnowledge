import { extendTheme, type ThemeConfig } from '@chakra-ui/react';

const colorModeConfig: ThemeConfig = {
  initialColorMode: 'system',
  useSystemColorMode: true,
};

export const theme = extendTheme({
  config: colorModeConfig,
  colors: {
    // Full scale: Chakra uses 200–400 for solid/ghost buttons and links in dark mode.
    brand: {
      50: '#f4f0fb',
      100: '#e2d7f5',
      200: '#c9b6ec',
      300: '#ad8fe0',
      400: '#8f69d2',
      500: '#6b46c1',
      600: '#553c9a',
      700: '#44337a',
      800: '#322659',
      900: '#21183c',
    },
  },
});
