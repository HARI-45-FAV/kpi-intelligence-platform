/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#07090d',
          900: '#0b0f16',
          850: '#10151f',
          800: '#161d29',
          700: '#1f2937',
          600: '#2b374a',
        },
        accent: {
          DEFAULT: '#4c8dff',
          soft: '#8fb6ff',
          dim: '#1d3a6b',
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
