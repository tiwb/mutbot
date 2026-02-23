import { Model, type IJsonModel } from "flexlayout-react";

export const PANEL_SESSION_LIST = "SessionList";
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
    borders: [
      {
        type: "border",
        location: "left",
        size: 260,
        children: [
          {
            type: "tab",
            name: "Sessions",
            component: PANEL_SESSION_LIST,
            enableClose: false,
          },
        ],
      },
    ],
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
  return Model.fromJson(json ?? createDefaultLayout());
}
