import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'
import externalGlobals from 'rollup-plugin-external-globals'

// Two builds from one codebase:
//   npm run build           -> dist/            served by the FastAPI backend (talks to /api)
//   npm run build:artifact  -> dist-artifact/   one self-contained HTML page with the
//                                               in-browser engine (hosted prototype)
const REACT_CDN = [
  'https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js',
]

export default defineConfig(({ mode }) => {
  const artifact = mode === 'artifact'
  return {
    base: './',
    plugins: [
      react({ jsxRuntime: 'classic' }),
      ...(artifact
        ? [
            viteSingleFile(),
            {
              name: 'react-from-cdn',
              transformIndexHtml: (html) =>
                html.replace('<!--react-cdn-->', REACT_CDN.map((src) => `<script src="${src}"></script>`).join('\n    ')),
            },
          ]
        : []),
    ],
    define: { __ARTIFACT__: JSON.stringify(artifact) },
    server: {
      fs: { allow: ['..'] },
      proxy: { '/api': 'http://localhost:8000' },
    },
    build: artifact
      ? {
          outDir: 'dist-artifact',
          rollupOptions: {
            external: ['react', 'react-dom', 'react-dom/client'],
            plugins: [externalGlobals({ react: 'React', 'react-dom': 'ReactDOM', 'react-dom/client': 'ReactDOM' })],
          },
        }
      : { outDir: 'dist' },
  }
})
