import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";

import { ErrorBoundary, type FallbackProps } from "react-error-boundary";

function ErrorFallback({ error }: FallbackProps) {
  return (
    <div style={{color: 'red', padding: '20px', backgroundColor: 'black', height: '100vh'}}>
      <h2>Crash!</h2>
      <pre>{error.message}</pre>
      <pre>{error.stack}</pre>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary FallbackComponent={ErrorFallback}>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
