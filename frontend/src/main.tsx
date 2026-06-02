import React from "react";
import ReactDOM from "react-dom/client";
import {
  Navigate,
  RouterProvider,
  createBrowserRouter,
} from "react-router-dom";

import { AppShell } from "@/components/AppShell";
import { FlowList } from "@/pages/FlowList";
import { FlowEditor } from "@/pages/FlowEditor";
import { RunList } from "@/pages/RunList";
import { RunDetail } from "@/pages/RunDetail";
import { ScheduledFlows } from "@/pages/ScheduledFlows";
import { OpenclawChat } from "@/pages/OpenclawChat";
import { Profiles } from "@/pages/Profiles";

import "@/i18n"; // initialise i18next before any component reads `t()`
import "./styles.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/flows" replace /> },
      { path: "flows", element: <FlowList /> },
      { path: "flows/new", element: <FlowEditor /> },
      { path: "flows/:id", element: <FlowEditor /> },
      { path: "runs", element: <RunList /> },
      { path: "scheduled-flows", element: <ScheduledFlows /> },
      { path: "runs/:id", element: <RunDetail /> },
      { path: "assistant", element: <Navigate to="/chat" replace /> },
      { path: "chat", element: <OpenclawChat /> },
      {
        path: "chat/store",
        element: <Navigate to={{ pathname: "/chat", search: "?storeComingSoon=1" }} replace />,
      },
      { path: "chat/:id", element: <OpenclawChat /> },
      // Back-compat for older bookmarks
      { path: "agents", element: <Navigate to="/chat" replace /> },
      { path: "agents/:id/chat", element: <OpenclawChat /> },
      { path: "profiles", element: <Profiles /> },
      { path: "*", element: <Navigate to="/flows" replace /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
