import { Command } from "cmdk";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { Search, Tornado, ShieldAlert, Sparkles, Clapperboard, MapPin } from "lucide-react";
import { SCREENS } from "../lib/screens";
import { useAegis } from "../lib/store";
import { network } from "../lib/data";
import { Kbd } from "./ui";

const item = "flex items-center gap-3 px-3 h-9 rounded-lg text-[13px] text-ink-2 cursor-pointer data-[selected=true]:bg-white/7 data-[selected=true]:text-ink";

export function CommandPalette() {
  const { paletteOpen, setPaletteOpen, setCopilotOpen, setScenario, triggerAttack, selectNode, setDirector } = useAegis();
  const navigate = useNavigate();
  const go = (fn: () => void) => { fn(); setPaletteOpen(false); };
  return (
    <Command.Dialog
      open={paletteOpen}
      onOpenChange={setPaletteOpen}
      label="Command palette"
      overlayClassName="fixed inset-0 z-[60] bg-black/50 backdrop-blur-sm"
      contentClassName="fixed z-[61] left-1/2 top-[18%] -translate-x-1/2 w-[560px] max-w-[92vw] glass !bg-[#0c1322]/95 shadow-2xl overflow-hidden"
    >
      <div className="flex items-center gap-2 px-4 border-b border-line">
        <Search size={15} className="text-ink-3" />
        <Command.Input placeholder="Jump to a screen, node or action…" className="flex-1 h-12 bg-transparent outline-none text-[14px] text-ink placeholder:text-ink-3" />
        <Kbd>esc</Kbd>
      </div>
      <Command.List className="max-h-[360px] overflow-y-auto scroll-thin p-2">
        <Command.Empty className="px-3 py-6 text-center text-[13px] text-ink-3">No results</Command.Empty>
        <Command.Group heading="Screens" className="[&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-2">
          {SCREENS.map((s) => (
            <Command.Item key={s.key} value={`screen ${s.label}`} className={item} onSelect={() => go(() => navigate(s.path))}>
              <s.icon size={15} /> {s.label} <span className="text-ink-3 text-[12px] truncate">{s.sub}</span>
              <span className="ml-auto"><Kbd>{s.shortcut}</Kbd></span>
            </Command.Item>
          ))}
        </Command.Group>
        <Command.Group heading="Actions" className="[&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-2">
          <Command.Item value="demo disruption cyclone chennai" className={item} onSelect={() => go(() => { navigate("/scenario"); setScenario({ status: "placed", type: "cyclone", target: "PORT_CHENNAI", progress: 0 }); })}>
            <Tornado size={15} className="text-warn" /> Place demo disruption: cyclone over Chennai <span className="ml-auto"><Kbd>D</Kbd></span>
          </Command.Item>
          <Command.Item value="chaos gps spoof attack" className={item} onSelect={() => go(() => { navigate("/trust"); triggerAttack("gps_teleport"); toast.error("GPS teleport injected", { description: "Truck R4471 → Mumbai" }); })}>
            <ShieldAlert size={15} className="text-bad" /> Inject attack: GPS teleport <span className="ml-auto"><Kbd>C</Kbd></span>
          </Command.Item>
          <Command.Item value="ask copilot ai" className={item} onSelect={() => go(() => setCopilotOpen(true))}>
            <Sparkles size={15} className="text-ai" /> Ask Copilot <span className="ml-auto"><Kbd>⌘J</Kbd></span>
          </Command.Item>
          <Command.Item value="director mode demo replay" className={item} onSelect={() => go(() => setDirector(true))}>
            <Clapperboard size={15} className="text-ai" /> Start Director mode <span className="ml-auto"><Kbd>F</Kbd></span>
          </Command.Item>
        </Command.Group>
        <Command.Group heading="Nodes" className="[&_[cmdk-group-heading]]:eyebrow [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-2">
          {network.nodes.filter((n) => n.type !== "zone").map((n) => (
            <Command.Item key={n.id} value={`node ${n.name} ${n.id}`} className={item} onSelect={() => go(() => { navigate("/"); selectNode(n.id); })}>
              <MapPin size={15} /> {n.name} <span className="num ml-auto text-[11px] text-ink-3">{n.id}</span>
            </Command.Item>
          ))}
        </Command.Group>
      </Command.List>
    </Command.Dialog>
  );
}
