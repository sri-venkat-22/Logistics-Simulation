import { lazy, Suspense } from "react";
import { HashRouter, Route, Routes } from "react-router";
import { Toaster } from "sonner";
import { Shell } from "./components/Shell";
import { Skeleton } from "./components/ui";
import ControlTower from "./screens/ControlTower";

const Intro = lazy(() => import("./screens/Intro"));
const ScenarioLab = lazy(() => import("./screens/ScenarioLab"));
const CityTwin = lazy(() => import("./screens/CityTwin"));
const TrustCenter = lazy(() => import("./screens/TrustCenter"));
const FidelityLab = lazy(() => import("./screens/FidelityLab"));
const NetworkGraph = lazy(() => import("./screens/NetworkGraph"));
const Ops = lazy(() => import("./screens/Ops"));

function Loading() {
  return (
    <div className="absolute inset-0 p-4 pt-16 grid grid-cols-4 grid-rows-[88px_1fr] gap-3">
      {[0, 1, 2, 3].map((i) => <Skeleton key={i} />)}
      <Skeleton className="col-span-4" />
    </div>
  );
}

export default function App() {
  return (
    <HashRouter>
      <Shell>
        <Suspense fallback={<Loading />}>
          <Routes>
            <Route path="/" element={<ControlTower />} />
            <Route path="/intro" element={<Intro />} />
            <Route path="/scenario" element={<ScenarioLab />} />
            <Route path="/city" element={<CityTwin />} />
            <Route path="/trust" element={<TrustCenter />} />
            <Route path="/fidelity" element={<FidelityLab />} />
            <Route path="/network" element={<NetworkGraph />} />
            <Route path="/ops" element={<Ops />} />
            <Route path="*" element={<ControlTower />} />
          </Routes>
        </Suspense>
      </Shell>
      <Toaster theme="dark" position="bottom-right" toastOptions={{ style: { background: "rgba(15,22,38,0.96)", border: "1px solid rgba(255,255,255,0.12)", color: "#E6EDF7", fontFamily: "Geist Variable, sans-serif" } }} />
    </HashRouter>
  );
}
