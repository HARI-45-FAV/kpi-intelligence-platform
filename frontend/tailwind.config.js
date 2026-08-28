/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#eaf5fc',
          900: '#f9fcff',
          850: '#f1f8fe',
          800: '#dcecf8',
          700: '#c7deef',
          600: '#9cbbd1',
        },
        accent: {
          DEFAULT: '#1978c5',
          soft: '#4b96d1',
          dim: '#d8edfc',
        },
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
