type FileWithDropPath = File & {
  path?: string;
  webkitRelativePath?: string;
};

type DropEntry = {
  isDirectory?: boolean;
  fullPath?: string;
  name?: string;
};

type DataTransferItemWithEntry = DataTransferItem & {
  webkitGetAsEntry?: () => DropEntry | null;
};

export type DroppedFolderPath = {
  hasFolder: boolean;
  absolutePath: string | null;
};

function normaliseSlashes(input: string): string {
  return input.replace(/\\/g, "/");
}

function isAbsolutePath(path: string): boolean {
  const value = path.trim();
  if (!value) return false;
  if (value.startsWith("/") || value.startsWith("//") || value.startsWith("\\\\")) return true;
  return /^[A-Za-z]:[\\/]/.test(value);
}

function dirnamePortable(path: string): string {
  const value = normaliseSlashes(path).replace(/\/+$/, "");
  if (!value) return value;
  if (/^[A-Za-z]:$/.test(value)) return `${value}/`;
  const idx = value.lastIndexOf("/");
  if (idx < 0) return value;
  if (idx === 0) return "/";
  const head = value.slice(0, idx);
  if (/^[A-Za-z]:$/.test(head)) return `${head}/`;
  return head;
}

function folderRootFromFile(file: FileWithDropPath): string | null {
  const absoluteFilePath = typeof file.path === "string" ? file.path.trim() : "";
  const relativePath = typeof file.webkitRelativePath === "string" ? file.webkitRelativePath.trim() : "";
  if (!absoluteFilePath || !relativePath.includes("/")) return null;
  if (!isAbsolutePath(absoluteFilePath)) return null;
  const parts = relativePath.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  let folderPath = dirnamePortable(absoluteFilePath);
  let levelsToAscend = Math.max(0, parts.length - 2);
  while (levelsToAscend > 0) {
    const next = dirnamePortable(folderPath);
    if (!next || next === folderPath) break;
    folderPath = next;
    levelsToAscend -= 1;
  }
  return folderPath;
}

function droppedContainsFolder(items: DataTransferItemList | null, files: FileWithDropPath[]): boolean {
  if (items) {
    for (const item of Array.from(items)) {
      const entry = (item as DataTransferItemWithEntry).webkitGetAsEntry?.();
      if (entry?.isDirectory) return true;
    }
  }
  return files.some((file) => (file.webkitRelativePath ?? "").includes("/"));
}

export function resolveDroppedFolderPath(dataTransfer: DataTransfer): DroppedFolderPath | null {
  const files = Array.from(dataTransfer.files ?? []).map((file) => file as FileWithDropPath);
  const items = dataTransfer.items ?? null;
  const hasFolder = droppedContainsFolder(items, files);
  if (!hasFolder) return null;
  for (const file of files) {
    const absoluteFolder = folderRootFromFile(file);
    if (absoluteFolder) return { hasFolder: true, absolutePath: absoluteFolder };
  }
  return { hasFolder: true, absolutePath: null };
}

