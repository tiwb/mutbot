import { Model, type IJsonModel } from "flexlayout-react";

export const PANEL_AGENT_CHAT = "AgentChat";
export const PANEL_TERMINAL = "Terminal";
export const PANEL_CODE_EDITOR = "CodeEditor";
export const PANEL_LOG = "Log";

export function createDefaultLayout(): IJsonModel {
  return {
    global: {
      tabEnableClose: true,
      tabSetEnableMaximize: true,
      splitterSize: 4,
    },
    borders: [],
    layout: {
      type: "row",
      weight: 100,
      children: [
        {
          type: "tabset",
          weight: 100,
          id: "main-tabset",
          children: [],
        },
      ],
    },
  };
}

export function createModel(json?: IJsonModel): Model {
  if (json) {
    // Normalize saved layout: strip legacy borders, enforce splitter config
    json = {
      ...json,
      borders: [],
      global: { ...json.global, splitterSize: 4, splitterExtra: 0 },
    };
  }
  return Model.fromJson(json ?? createDefaultLayout());
}
