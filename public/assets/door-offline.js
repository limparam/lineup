const DB_NAME = "lineup-door";
const DB_VERSION = 1;

function openDb() {
  return new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) {
      reject(new Error("IndexedDB недоступен"));
      return;
    }
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains("snapshots")) {
        db.createObjectStore("snapshots", { keyPath: "token" });
      }
      if (!db.objectStoreNames.contains("outbox")) {
        const store = db.createObjectStore("outbox", { keyPath: "id" });
        store.createIndex("token", "token", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("Ошибка IndexedDB"));
  });
}

async function withStore(name, mode, action) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(name, mode);
    const store = tx.objectStore(name);
    let result;
    try {
      result = action(store);
    } catch (error) {
      db.close();
      reject(error);
      return;
    }
    tx.oncomplete = () => {
      db.close();
      resolve(result);
    };
    tx.onerror = () => {
      db.close();
      reject(tx.error || new Error("Ошибка IndexedDB"));
    };
    tx.onabort = () => {
      db.close();
      reject(tx.error || new Error("Транзакция IndexedDB отменена"));
    };
  });
}

export async function saveSnapshot(token, snapshot) {
  const record = {
    ...snapshot,
    token,
    savedAt: Date.now(),
  };
  await withStore("snapshots", "readwrite", (store) => store.put(record));
  return record;
}

export async function loadSnapshot(token) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("snapshots", "readonly");
    const request = tx.objectStore("snapshots").get(token);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
    tx.onerror = () => {
      db.close();
      reject(tx.error);
    };
  });
}

export async function enqueueOperation(token, operation) {
  const record = {
    ...operation,
    token,
    createdAt: operation.at || new Date().toISOString(),
  };
  await withStore("outbox", "readwrite", (store) => store.put(record));
  return record;
}

export async function pendingOperations(token) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("outbox", "readonly");
    const store = tx.objectStore("outbox");
    const request = store.index("token").getAll(token);
    request.onsuccess = () => {
      const rows = request.result || [];
      rows.sort((a, b) => String(a.createdAt).localeCompare(String(b.createdAt)));
      resolve(rows);
    };
    request.onerror = () => reject(request.error);
    tx.oncomplete = () => db.close();
    tx.onerror = () => {
      db.close();
      reject(tx.error);
    };
  });
}

export async function removeOperations(ids) {
  if (!ids.length) return;
  await withStore("outbox", "readwrite", (store) => {
    for (const id of ids) store.delete(id);
  });
}
