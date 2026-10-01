import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";

import { ErrorBoundary, type FallbackProps } from "react-error-boundary";

function ErrorFallback({ error }: FallbackProps) {
  const errorMessage = error instanceof Error ? error.message : String(error);
  const errorStack = error instanceof Error ? error.stack : '';
  
  return (
    <div style={{color: 'red', padding: '20px', backgroundColor: 'black', height: '100vh'}}>
      <h2>Crash!</h2>
      <pre>{errorMessage}</pre>
      <pre>{errorStack}</pre>
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
