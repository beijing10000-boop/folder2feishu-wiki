import { AppHeader } from "./components/AppHeader";
import { BackgroundTaskBar, BootError, BootScreen, StatusBar, Toast } from "./components/shell";
import { ConsoleProvider } from "./hooks/consoleContext";
import { useMigrationConsole } from "./hooks/useMigrationConsole";
import { steps } from "./lib/steps";
import { ConfigStep } from "./steps/ConfigStep";
import { PlanStep } from "./steps/PlanStep";
import { PreflightStep } from "./steps/PreflightStep";
import { RunStep } from "./steps/RunStep";
import { ScanStep } from "./steps/ScanStep";

const stepViews = {
  config: ConfigStep,
  scan: ScanStep,
  preflight: PreflightStep,
  plan: PlanStep,
  run: RunStep
} as const;

function App() {
  const console = useMigrationConsole();
  const { booting, bootError, step, project } = console;

  if (booting) return <BootScreen />;
  if (bootError) return <BootError message={bootError} />;

  const activeStep = steps.find((item) => item.id === step) ?? steps[0];
  const StepView = stepViews[step];

  return (
    <ConsoleProvider value={console}>
      <div className="app-shell">
        <AppHeader />

        <main id="main" className="workspace">
          <div className="page-head">
            <div className="page-head__text">
              <span className="eyebrow">
                {activeStep.no} / 05 · {activeStep.eyebrow}
              </span>
              <h1>{activeStep.label}</h1>
              <p>{activeStep.description}</p>
            </div>
            <div className={`project-plate ${project ? "" : "is-empty"}`.trim()}>
              <span>当前项目</span>
              <strong>{project ? project.name : "尚未创建迁移项目"}</strong>
              <small>{project ? `项目编号 · ${project.id}` : "完成配置验证后生成台账"}</small>
            </div>
          </div>

          <BackgroundTaskBar />
          <StepView />
        </main>

        <StatusBar />
        <Toast />
      </div>
    </ConsoleProvider>
  );
}

export default App;
