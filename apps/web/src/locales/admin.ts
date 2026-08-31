const es = {
  adminConfig: {
    tabs: { general: "General", identity: "Identidad", tools: "Herramientas", integrations: "Integraciones", secrets: "Secretos", management: "Administrar agente" },
    common: {
      editable: "editable", readOnly: "solo lectura", configured: "configurado", incomplete: "incompleto", active: "activo", inactive: "inactivo", protected: "protegido",
      updated: "{{name}} actualizado.", deleted: "{{name}} eliminado.", testComplete: "Prueba de {{name}} completada.",
    },
    models: {
      title: "Modelo principal", description: "Selección detectada por Hermes para este perfil.", provider: "Proveedor", model: "Modelo",
      confirmExpensive: "Confirmo el cambio si el modelo tiene un costo superior.", save: "Guardar modelo", empty: "Hermes no devolvió opciones de modelo.", updated: "Modelo actualizado.",
    },
    config: {
      title: "Configuración compatible", description: "Documento saneado del perfil. Los campos con forma de secreto son rechazados por la API.", advanced: "avanzado", json: "Configuración JSON",
      invalidJson: "Escribe un objeto JSON válido. Los secretos deben configurarse en su sección de solo escritura.", apply: "Aplicar configuración", applied: "Configuración aplicada.",
    },
    usage: { title: "Uso y contexto", description: "Ventana de {{days}} días informada por Hermes.", input: "Entrada", output: "Salida", sessions: "Sesiones" },
    soul: { title: "SOUL", description: "Identidad e instrucciones oficiales del perfil, gestionadas en Hermes.", content: "Contenido de SOUL", save: "Guardar SOUL", updated: "SOUL actualizado." },
    collections: { empty: "No hay elementos anunciados para este perfil.", skillsTitle: "Skills", skillsDescription: "Habilidades descubiertas en el perfil.", toolsetsTitle: "Toolsets", toolsetsDescription: "Conjuntos de herramientas administrados por Hermes." },
    mcp: {
      title: "Servidores MCP", description: "Conexiones detectadas para el perfil. Los tokens solo se escriben y se vacían al terminar.", managedTransport: "Transporte administrado",
      activate: "Activar {{name}}", test: "Probar", delete: "Eliminar", deleteConfirm: "¿Eliminar el servidor MCP {{name}}?", empty: "No hay servidores MCP configurados.",
      addTitle: "Añadir servidor", name: "Nombre", url: "URL", localCommand: "Comando local", bearerToken: "Bearer token (solo escritura)", validation: "Indica un nombre y exactamente una conexión: URL o comando.", add: "Añadir MCP", added: "Servidor MCP añadido.",
    },
    channels: {
      title: "Canales", description: "Canales y credenciales administrados por Hermes.", requiresConfiguration: "requiere configuración", activate: "Activar {{name}}",
      writeOnly: "{{key}} (solo escritura)", configuredReplace: "Configurado · escribe para reemplazar", unconfigured: "Sin configurar", clearValue: "Borrar valor", clearConfirm: "¿Borrar {{key}} de {{name}}?",
      saveCredentials: "Guardar credenciales", test: "Probar canal", empty: "No hay canales anunciados.",
    },
    secrets: {
      title: "Secretos de solo escritura", description: "La interfaz solo conoce si un valor está configurado. Nunca recibe ni muestra su contenido.", managedValue: "Valor administrado por Hermes",
      newValue: "Nuevo valor para {{name}}", save: "Guardar valor", delete: "Eliminar", deleteConfirm: "¿Eliminar {{name}}? Hermes dejará de tener acceso a este valor.", empty: "Hermes no anunció secretos configurables.", saved: "{{name}} guardado sin exponer su valor.",
    },
    management: {
      title: "Administrar agente", description: "Mueve este agente a otro gateway o elimínalo de Hermes.", protected: "protegido", managed: "administrado",
      agent: "Agente", currentGateway: "Gateway actual", transferDisclosure: "Al moverlo se conservan identidad, memoria, sesiones, skills y automatizaciones cron. Las credenciales no se copian; las rutas y herramientas locales pueden requerir ajustes en el gateway destino.",
      defaultProtected: "El perfil default es esencial para administrar Hermes y no se puede mover ni eliminar desde Agent Control.",
      reconcileBlocked: "Las operaciones de este agente están bloqueadas en este dispositivo hasta recargar Agent Control y verificar su estado.",
      moveTitle: "Mover a otro gateway", moveDescription: "Transfiere el agente y conserva su contenido en un gateway compatible.", noDestination: "No hay otro gateway compatible con importación y rollback/eliminación de perfiles verificados.", move: "Mover agente",
      deleteTitle: "Eliminar agente", deleteDescription: "Elimina de forma permanente el perfil y los datos asociados que Hermes indique.", delete: "Eliminar agente",
      warningsTitle: "La operación terminó con avisos", moveEyebrow: "Transferencia de perfil", moveDialogTitle: "Mover a {{name}}", moveDialogDescription: "Elige el gateway destino y confirma con el nombre técnico exacto.", destinationGateway: "Gateway destino",
      typeToConfirm: "Escribe {{name}} para confirmar", cancel: "Cancelar", cancelMove: "Cancelar transferencia", moving: "Moviendo…", confirmMove: "Confirmar transferencia",
      deleteEyebrow: "Acción irreversible", deleteDialogTitle: "Eliminar a {{name}}", deleteDialogDescription: "Hermes eliminará este agente. Esta acción no se puede deshacer; confirma con el nombre técnico exacto.", cancelDelete: "Cancelar eliminación", deleting: "Eliminando…", confirmDelete: "Eliminar definitivamente",
      moved: "{{name}} se movió al gateway destino.", deleted: "{{name}} se eliminó.", localCacheWarning: "No se pudo limpiar por completo el caché local de este dispositivo; bórralo desde Ajustes.", committedRefreshError: "El agente ya fue {{action}}, pero no se pudo actualizar esta pantalla. Recarga Agent Control.", outcomeUnknown: "Hermes no confirmó el resultado. Agent Control reconcilió el estado disponible y bloqueó el reenvío. Verifica dónde está el agente antes de iniciar otra operación.", outcomeUnknownRefresh: "Hermes no confirmó el resultado y Agent Control no pudo reconciliarlo. No repitas la operación: recarga Agent Control y verifica el agente primero.", movedAction: "movido", deletedAction: "eliminado",
    },
    route: { aria: "Destino de configuración", gateway: "Gateway", profile: "Perfil Hermes", contract: "Contrato", unverified: "sin verificar", exactMethods: "{{count}} métodos exactos" },
    offline: "Administración bloqueada sin conexión. Los datos visibles pueden estar desactualizados.",
    errors: { read: "No se pudo leer la configuración de Hermes.", rejected: "Hermes rechazó la operación.", heading: "No se completó la operación" },
    unavailable: { title: "Sin funciones administrativas verificadas", selectedProfile: "seleccionado", body: "El perfil {{profile}} no anunció métodos exactos de administración. Agent Control mantiene ocultos todos los controles." },
    loading: "Consultando Hermes…",
  },
} as const;

const en = {
  adminConfig: {
    tabs: { general: "General", identity: "Identity", tools: "Tools", integrations: "Integrations", secrets: "Secrets", management: "Manage agent" },
    common: {
      editable: "editable", readOnly: "read only", configured: "configured", incomplete: "incomplete", active: "active", inactive: "inactive", protected: "protected",
      updated: "{{name}} updated.", deleted: "{{name}} deleted.", testComplete: "{{name}} test completed.",
    },
    models: {
      title: "Primary model", description: "The model selection reported by Hermes for this profile.", provider: "Provider", model: "Model",
      confirmExpensive: "I confirm the change if the model has a higher cost.", save: "Save model", empty: "Hermes did not return any model options.", updated: "Model updated.",
    },
    config: {
      title: "Compatible configuration", description: "Sanitized profile document. Fields that resemble secrets are rejected by the API.", advanced: "advanced", json: "JSON configuration",
      invalidJson: "Enter a valid JSON object. Secrets must be configured in their write-only section.", apply: "Apply configuration", applied: "Configuration applied.",
    },
    usage: { title: "Usage and context", description: "{{days}}-day window reported by Hermes.", input: "Input", output: "Output", sessions: "Sessions" },
    soul: { title: "SOUL", description: "The profile's official identity and instructions, managed in Hermes.", content: "SOUL content", save: "Save SOUL", updated: "SOUL updated." },
    collections: { empty: "No items were reported for this profile.", skillsTitle: "Skills", skillsDescription: "Skills discovered in the profile.", toolsetsTitle: "Toolsets", toolsetsDescription: "Tool collections managed by Hermes." },
    mcp: {
      title: "MCP servers", description: "Connections detected for this profile. Tokens are write-only and cleared when the operation finishes.", managedTransport: "Managed transport",
      activate: "Enable {{name}}", test: "Test", delete: "Delete", deleteConfirm: "Delete the MCP server {{name}}?", empty: "No MCP servers are configured.",
      addTitle: "Add server", name: "Name", url: "URL", localCommand: "Local command", bearerToken: "Bearer token (write only)", validation: "Enter a name and exactly one connection: URL or command.", add: "Add MCP", added: "MCP server added.",
    },
    channels: {
      title: "Channels", description: "Channels and credentials managed by Hermes.", requiresConfiguration: "configuration required", activate: "Enable {{name}}",
      writeOnly: "{{key}} (write only)", configuredReplace: "Configured · enter a value to replace", unconfigured: "Not configured", clearValue: "Clear value", clearConfirm: "Clear {{key}} from {{name}}?",
      saveCredentials: "Save credentials", test: "Test channel", empty: "No channels were reported.",
    },
    secrets: {
      title: "Write-only secrets", description: "The interface only knows whether a value is configured. It never receives or displays its contents.", managedValue: "Value managed by Hermes",
      newValue: "New value for {{name}}", save: "Save value", delete: "Delete", deleteConfirm: "Delete {{name}}? Hermes will no longer have access to this value.", empty: "Hermes did not report any configurable secrets.", saved: "{{name}} saved without exposing its value.",
    },
    management: {
      title: "Manage agent", description: "Move this agent to another gateway or delete it from Hermes.", protected: "protected", managed: "managed",
      agent: "Agent", currentGateway: "Current gateway", transferDisclosure: "Moving preserves identity, memory, sessions, skills, and cron automations. Credentials are not copied; local routes and tools may need adjustment on the destination gateway.",
      defaultProtected: "The default profile is essential for administering Hermes and cannot be moved or deleted from Agent Control.",
      reconcileBlocked: "Operations for this agent are blocked on this device until you reload Agent Control and verify its state.",
      moveTitle: "Move to another gateway", moveDescription: "Transfer the agent and preserve its content on a compatible gateway.", noDestination: "No other compatible gateway has verified profile import and rollback/deletion support.", move: "Move agent",
      deleteTitle: "Delete agent", deleteDescription: "Permanently delete the profile and the associated data reported by Hermes.", delete: "Delete agent",
      warningsTitle: "The operation completed with warnings", moveEyebrow: "Profile transfer", moveDialogTitle: "Move {{name}}", moveDialogDescription: "Choose the destination gateway and confirm with the exact technical name.", destinationGateway: "Destination gateway",
      typeToConfirm: "Type {{name}} to confirm", cancel: "Cancel", cancelMove: "Cancel transfer", moving: "Moving…", confirmMove: "Confirm transfer",
      deleteEyebrow: "Irreversible action", deleteDialogTitle: "Delete {{name}}", deleteDialogDescription: "Hermes will delete this agent. This cannot be undone; confirm with the exact technical name.", cancelDelete: "Cancel deletion", deleting: "Deleting…", confirmDelete: "Delete permanently",
      moved: "{{name}} was moved to the destination gateway.", deleted: "{{name}} was deleted.", localCacheWarning: "This device's local cache could not be fully cleared; clear it from Settings.", committedRefreshError: "The agent was already {{action}}, but this screen could not be refreshed. Reload Agent Control.", outcomeUnknown: "Hermes did not confirm the outcome. Agent Control reconciled the available state and blocked resubmission. Verify where the agent is before starting another operation.", outcomeUnknownRefresh: "Hermes did not confirm the outcome and Agent Control could not reconcile it. Do not repeat the operation: reload Agent Control and verify the agent first.", movedAction: "moved", deletedAction: "deleted",
    },
    route: { aria: "Configuration target", gateway: "Gateway", profile: "Hermes profile", contract: "Contract", unverified: "unverified", exactMethods: "{{count}} exact methods" },
    offline: "Administration is blocked while offline. Visible data may be out of date.",
    errors: { read: "The Hermes configuration could not be read.", rejected: "Hermes rejected the operation.", heading: "The operation could not be completed" },
    unavailable: { title: "No verified administration features", selectedProfile: "selected", body: "The {{profile}} profile did not report exact administration methods. Agent Control keeps all controls hidden." },
    loading: "Querying Hermes…",
  },
} as const;

const fr = {
  adminConfig: {
    tabs: { general: "Général", identity: "Identité", tools: "Outils", integrations: "Intégrations", secrets: "Secrets", management: "Gérer l’agent" },
    common: {
      editable: "modifiable", readOnly: "lecture seule", configured: "configuré", incomplete: "incomplet", active: "actif", inactive: "inactif", protected: "protégé",
      updated: "{{name}} mis à jour.", deleted: "{{name}} supprimé.", testComplete: "Test de {{name}} terminé.",
    },
    models: {
      title: "Modèle principal", description: "Sélection de modèle signalée par Hermes pour ce profil.", provider: "Fournisseur", model: "Modèle",
      confirmExpensive: "Je confirme le changement si le modèle est plus coûteux.", save: "Enregistrer le modèle", empty: "Hermes n’a renvoyé aucune option de modèle.", updated: "Modèle mis à jour.",
    },
    config: {
      title: "Configuration compatible", description: "Document de profil assaini. Les champs ressemblant à des secrets sont rejetés par l’API.", advanced: "avancé", json: "Configuration JSON",
      invalidJson: "Saisissez un objet JSON valide. Les secrets doivent être configurés dans leur section en écriture seule.", apply: "Appliquer la configuration", applied: "Configuration appliquée.",
    },
    usage: { title: "Utilisation et contexte", description: "Fenêtre de {{days}} jours signalée par Hermes.", input: "Entrée", output: "Sortie", sessions: "Sessions" },
    soul: { title: "SOUL", description: "Identité et instructions officielles du profil, gérées dans Hermes.", content: "Contenu de SOUL", save: "Enregistrer SOUL", updated: "SOUL mis à jour." },
    collections: { empty: "Aucun élément signalé pour ce profil.", skillsTitle: "Skills", skillsDescription: "Compétences détectées dans le profil.", toolsetsTitle: "Toolsets", toolsetsDescription: "Ensembles d’outils gérés par Hermes." },
    mcp: {
      title: "Serveurs MCP", description: "Connexions détectées pour ce profil. Les jetons sont en écriture seule et effacés à la fin de l’opération.", managedTransport: "Transport géré",
      activate: "Activer {{name}}", test: "Tester", delete: "Supprimer", deleteConfirm: "Supprimer le serveur MCP {{name}} ?", empty: "Aucun serveur MCP configuré.",
      addTitle: "Ajouter un serveur", name: "Nom", url: "URL", localCommand: "Commande locale", bearerToken: "Jeton Bearer (écriture seule)", validation: "Indiquez un nom et exactement une connexion : URL ou commande.", add: "Ajouter MCP", added: "Serveur MCP ajouté.",
    },
    channels: {
      title: "Canaux", description: "Canaux et identifiants gérés par Hermes.", requiresConfiguration: "configuration requise", activate: "Activer {{name}}",
      writeOnly: "{{key}} (écriture seule)", configuredReplace: "Configuré · saisissez une valeur pour remplacer", unconfigured: "Non configuré", clearValue: "Effacer la valeur", clearConfirm: "Effacer {{key}} de {{name}} ?",
      saveCredentials: "Enregistrer les identifiants", test: "Tester le canal", empty: "Aucun canal signalé.",
    },
    secrets: {
      title: "Secrets en écriture seule", description: "L’interface sait uniquement si une valeur est configurée. Elle ne reçoit ni n’affiche jamais son contenu.", managedValue: "Valeur gérée par Hermes",
      newValue: "Nouvelle valeur pour {{name}}", save: "Enregistrer la valeur", delete: "Supprimer", deleteConfirm: "Supprimer {{name}} ? Hermes n’aura plus accès à cette valeur.", empty: "Hermes n’a signalé aucun secret configurable.", saved: "{{name}} enregistré sans exposer sa valeur.",
    },
    management: {
      title: "Gérer l’agent", description: "Déplacez cet agent vers une autre gateway ou supprimez-le de Hermes.", protected: "protégé", managed: "géré",
      agent: "Agent", currentGateway: "Gateway actuelle", transferDisclosure: "Le déplacement conserve l’identité, la mémoire, les sessions, les skills et les automatisations cron. Les identifiants ne sont pas copiés ; les chemins et outils locaux peuvent nécessiter des ajustements sur la gateway de destination.",
      defaultProtected: "Le profil default est essentiel à l’administration de Hermes et ne peut pas être déplacé ni supprimé depuis Agent Control.",
      reconcileBlocked: "Les opérations de cet agent sont bloquées sur cet appareil jusqu’au rechargement de Agent Control et à la vérification de son état.",
      moveTitle: "Déplacer vers une autre gateway", moveDescription: "Transférez l’agent et conservez son contenu sur une gateway compatible.", noDestination: "Aucune autre gateway compatible ne dispose d’un import et d’un rollback/suppression de profils vérifiés.", move: "Déplacer l’agent",
      deleteTitle: "Supprimer l’agent", deleteDescription: "Supprimez définitivement le profil et les données associées signalées par Hermes.", delete: "Supprimer l’agent",
      warningsTitle: "L’opération s’est terminée avec des avertissements", moveEyebrow: "Transfert de profil", moveDialogTitle: "Déplacer {{name}}", moveDialogDescription: "Choisissez la gateway de destination et confirmez avec le nom technique exact.", destinationGateway: "Gateway de destination",
      typeToConfirm: "Saisissez {{name}} pour confirmer", cancel: "Annuler", cancelMove: "Annuler le transfert", moving: "Déplacement…", confirmMove: "Confirmer le transfert",
      deleteEyebrow: "Action irréversible", deleteDialogTitle: "Supprimer {{name}}", deleteDialogDescription: "Hermes supprimera cet agent. Cette action est irréversible ; confirmez avec le nom technique exact.", cancelDelete: "Annuler la suppression", deleting: "Suppression…", confirmDelete: "Supprimer définitivement",
      moved: "{{name}} a été déplacé vers la gateway de destination.", deleted: "{{name}} a été supprimé.", localCacheWarning: "Le cache local de cet appareil n’a pas pu être entièrement vidé ; effacez-le dans les réglages.", committedRefreshError: "L’agent a déjà été {{action}}, mais cet écran n’a pas pu être actualisé. Rechargez Agent Control.", outcomeUnknown: "Hermes n’a pas confirmé le résultat. Agent Control a rapproché l’état disponible et bloqué le renvoi. Vérifiez où se trouve l’agent avant toute autre opération.", outcomeUnknownRefresh: "Hermes n’a pas confirmé le résultat et Agent Control n’a pas pu le rapprocher. Ne répétez pas l’opération : rechargez Agent Control et vérifiez d’abord l’agent.", movedAction: "déplacé", deletedAction: "supprimé",
    },
    route: { aria: "Cible de configuration", gateway: "Gateway", profile: "Profil Hermes", contract: "Contrat", unverified: "non vérifié", exactMethods: "{{count}} méthodes exactes" },
    offline: "L’administration est bloquée hors ligne. Les données visibles peuvent être obsolètes.",
    errors: { read: "Impossible de lire la configuration Hermes.", rejected: "Hermes a rejeté l’opération.", heading: "L’opération n’a pas pu être terminée" },
    unavailable: { title: "Aucune fonction d’administration vérifiée", selectedProfile: "sélectionné", body: "Le profil {{profile}} n’a signalé aucune méthode d’administration exacte. Agent Control masque tous les contrôles." },
    loading: "Interrogation de Hermes…",
  },
} as const;

const de = {
  adminConfig: {
    tabs: { general: "Allgemein", identity: "Identität", tools: "Werkzeuge", integrations: "Integrationen", secrets: "Geheimnisse", management: "Agent verwalten" },
    common: {
      editable: "bearbeitbar", readOnly: "schreibgeschützt", configured: "konfiguriert", incomplete: "unvollständig", active: "aktiv", inactive: "inaktiv", protected: "geschützt",
      updated: "{{name}} aktualisiert.", deleted: "{{name}} gelöscht.", testComplete: "Test von {{name}} abgeschlossen.",
    },
    models: {
      title: "Primäres Modell", description: "Von Hermes gemeldete Modellauswahl für dieses Profil.", provider: "Anbieter", model: "Modell",
      confirmExpensive: "Ich bestätige die Änderung, falls das Modell höhere Kosten verursacht.", save: "Modell speichern", empty: "Hermes hat keine Modelloptionen zurückgegeben.", updated: "Modell aktualisiert.",
    },
    config: {
      title: "Kompatible Konfiguration", description: "Bereinigtes Profildokument. Felder, die Geheimnissen ähneln, werden von der API abgelehnt.", advanced: "erweitert", json: "JSON-Konfiguration",
      invalidJson: "Geben Sie ein gültiges JSON-Objekt ein. Geheimnisse müssen im Bereich für nur beschreibbare Werte konfiguriert werden.", apply: "Konfiguration anwenden", applied: "Konfiguration angewendet.",
    },
    usage: { title: "Nutzung und Kontext", description: "Von Hermes gemeldetes Zeitfenster von {{days}} Tagen.", input: "Eingabe", output: "Ausgabe", sessions: "Sitzungen" },
    soul: { title: "SOUL", description: "Offizielle Identität und Anweisungen des Profils, verwaltet in Hermes.", content: "SOUL-Inhalt", save: "SOUL speichern", updated: "SOUL aktualisiert." },
    collections: { empty: "Für dieses Profil wurden keine Elemente gemeldet.", skillsTitle: "Skills", skillsDescription: "Im Profil erkannte Fähigkeiten.", toolsetsTitle: "Toolsets", toolsetsDescription: "Von Hermes verwaltete Werkzeugsammlungen." },
    mcp: {
      title: "MCP-Server", description: "Für dieses Profil erkannte Verbindungen. Token sind nur beschreibbar und werden nach dem Vorgang gelöscht.", managedTransport: "Verwalteter Transport",
      activate: "{{name}} aktivieren", test: "Testen", delete: "Löschen", deleteConfirm: "MCP-Server {{name}} löschen?", empty: "Keine MCP-Server konfiguriert.",
      addTitle: "Server hinzufügen", name: "Name", url: "URL", localCommand: "Lokaler Befehl", bearerToken: "Bearer-Token (nur schreiben)", validation: "Geben Sie einen Namen und genau eine Verbindung an: URL oder Befehl.", add: "MCP hinzufügen", added: "MCP-Server hinzugefügt.",
    },
    channels: {
      title: "Kanäle", description: "Von Hermes verwaltete Kanäle und Zugangsdaten.", requiresConfiguration: "Konfiguration erforderlich", activate: "{{name}} aktivieren",
      writeOnly: "{{key}} (nur schreiben)", configuredReplace: "Konfiguriert · zum Ersetzen Wert eingeben", unconfigured: "Nicht konfiguriert", clearValue: "Wert löschen", clearConfirm: "{{key}} aus {{name}} löschen?",
      saveCredentials: "Zugangsdaten speichern", test: "Kanal testen", empty: "Keine Kanäle gemeldet.",
    },
    secrets: {
      title: "Nur beschreibbare Geheimnisse", description: "Die Oberfläche weiß nur, ob ein Wert konfiguriert ist. Sein Inhalt wird niemals empfangen oder angezeigt.", managedValue: "Von Hermes verwalteter Wert",
      newValue: "Neuer Wert für {{name}}", save: "Wert speichern", delete: "Löschen", deleteConfirm: "{{name}} löschen? Hermes hat danach keinen Zugriff mehr auf diesen Wert.", empty: "Hermes hat keine konfigurierbaren Geheimnisse gemeldet.", saved: "{{name}} gespeichert, ohne den Wert offenzulegen.",
    },
    management: {
      title: "Agent verwalten", description: "Verschieben Sie diesen Agenten auf ein anderes Gateway oder löschen Sie ihn aus Hermes.", protected: "geschützt", managed: "verwaltet",
      agent: "Agent", currentGateway: "Aktuelles Gateway", transferDisclosure: "Beim Verschieben bleiben Identität, Speicher, Sitzungen, Skills und Cron-Automatisierungen erhalten. Zugangsdaten werden nicht kopiert; lokale Pfade und Werkzeuge müssen am Ziel-Gateway möglicherweise angepasst werden.",
      defaultProtected: "Das Profil default ist für die Verwaltung von Hermes unverzichtbar und kann in Agent Control weder verschoben noch gelöscht werden.",
      reconcileBlocked: "Vorgänge für diesen Agenten sind auf diesem Gerät gesperrt, bis Agent Control neu geladen und sein Status geprüft wurde.",
      moveTitle: "Auf ein anderes Gateway verschieben", moveDescription: "Übertragen Sie den Agenten samt Inhalt auf ein kompatibles Gateway.", noDestination: "Kein anderes kompatibles Gateway verfügt über verifizierten Profilimport und verifiziertes Rollback/Löschen.", move: "Agent verschieben",
      deleteTitle: "Agent löschen", deleteDescription: "Löscht das Profil und die von Hermes gemeldeten zugehörigen Daten dauerhaft.", delete: "Agent löschen",
      warningsTitle: "Der Vorgang wurde mit Warnungen abgeschlossen", moveEyebrow: "Profilübertragung", moveDialogTitle: "{{name}} verschieben", moveDialogDescription: "Wählen Sie das Ziel-Gateway und bestätigen Sie mit dem exakten technischen Namen.", destinationGateway: "Ziel-Gateway",
      typeToConfirm: "Geben Sie zur Bestätigung {{name}} ein", cancel: "Abbrechen", cancelMove: "Übertragung abbrechen", moving: "Wird verschoben…", confirmMove: "Übertragung bestätigen",
      deleteEyebrow: "Unwiderrufliche Aktion", deleteDialogTitle: "{{name}} löschen", deleteDialogDescription: "Hermes löscht diesen Agenten. Dies kann nicht rückgängig gemacht werden; bestätigen Sie mit dem exakten technischen Namen.", cancelDelete: "Löschen abbrechen", deleting: "Wird gelöscht…", confirmDelete: "Endgültig löschen",
      moved: "{{name}} wurde auf das Ziel-Gateway verschoben.", deleted: "{{name}} wurde gelöscht.", localCacheWarning: "Der lokale Cache dieses Geräts konnte nicht vollständig geleert werden; löschen Sie ihn in den Einstellungen.", committedRefreshError: "Der Agent wurde bereits {{action}}, aber diese Ansicht konnte nicht aktualisiert werden. Laden Sie Agent Control neu.", outcomeUnknown: "Hermes hat das Ergebnis nicht bestätigt. Agent Control hat den verfügbaren Stand abgeglichen und ein erneutes Senden blockiert. Prüfen Sie den Standort des Agenten vor einem weiteren Vorgang.", outcomeUnknownRefresh: "Hermes hat das Ergebnis nicht bestätigt und Agent Control konnte es nicht abgleichen. Wiederholen Sie den Vorgang nicht: Laden Sie Agent Control neu und prüfen Sie zuerst den Agenten.", movedAction: "verschoben", deletedAction: "gelöscht",
    },
    route: { aria: "Konfigurationsziel", gateway: "Gateway", profile: "Hermes-Profil", contract: "Vertrag", unverified: "nicht verifiziert", exactMethods: "{{count}} exakte Methoden" },
    offline: "Die Verwaltung ist offline gesperrt. Sichtbare Daten können veraltet sein.",
    errors: { read: "Die Hermes-Konfiguration konnte nicht gelesen werden.", rejected: "Hermes hat den Vorgang abgelehnt.", heading: "Der Vorgang konnte nicht abgeschlossen werden" },
    unavailable: { title: "Keine verifizierten Verwaltungsfunktionen", selectedProfile: "ausgewählt", body: "Das Profil {{profile}} hat keine exakten Verwaltungsmethoden gemeldet. Agent Control blendet alle Steuerelemente aus." },
    loading: "Hermes wird abgefragt…",
  },
} as const;

const pt = {
  adminConfig: {
    tabs: { general: "Geral", identity: "Identidade", tools: "Ferramentas", integrations: "Integrações", secrets: "Segredos", management: "Gerenciar agente" },
    common: {
      editable: "editável", readOnly: "somente leitura", configured: "configurado", incomplete: "incompleto", active: "ativo", inactive: "inativo", protected: "protegido",
      updated: "{{name}} atualizado.", deleted: "{{name}} excluído.", testComplete: "Teste de {{name}} concluído.",
    },
    models: {
      title: "Modelo principal", description: "Seleção de modelo informada pelo Hermes para este perfil.", provider: "Provedor", model: "Modelo",
      confirmExpensive: "Confirmo a alteração caso o modelo tenha um custo maior.", save: "Salvar modelo", empty: "O Hermes não retornou opções de modelo.", updated: "Modelo atualizado.",
    },
    config: {
      title: "Configuração compatível", description: "Documento sanitizado do perfil. Campos que se parecem com segredos são rejeitados pela API.", advanced: "avançado", json: "Configuração JSON",
      invalidJson: "Insira um objeto JSON válido. Os segredos devem ser configurados na seção somente para gravação.", apply: "Aplicar configuração", applied: "Configuração aplicada.",
    },
    usage: { title: "Uso e contexto", description: "Janela de {{days}} dias informada pelo Hermes.", input: "Entrada", output: "Saída", sessions: "Sessões" },
    soul: { title: "SOUL", description: "Identidade e instruções oficiais do perfil, gerenciadas no Hermes.", content: "Conteúdo do SOUL", save: "Salvar SOUL", updated: "SOUL atualizado." },
    collections: { empty: "Nenhum item informado para este perfil.", skillsTitle: "Skills", skillsDescription: "Habilidades encontradas no perfil.", toolsetsTitle: "Toolsets", toolsetsDescription: "Conjuntos de ferramentas gerenciados pelo Hermes." },
    mcp: {
      title: "Servidores MCP", description: "Conexões detectadas para este perfil. Os tokens são somente para gravação e apagados ao final da operação.", managedTransport: "Transporte gerenciado",
      activate: "Ativar {{name}}", test: "Testar", delete: "Excluir", deleteConfirm: "Excluir o servidor MCP {{name}}?", empty: "Nenhum servidor MCP configurado.",
      addTitle: "Adicionar servidor", name: "Nome", url: "URL", localCommand: "Comando local", bearerToken: "Bearer token (somente gravação)", validation: "Informe um nome e exatamente uma conexão: URL ou comando.", add: "Adicionar MCP", added: "Servidor MCP adicionado.",
    },
    channels: {
      title: "Canais", description: "Canais e credenciais gerenciados pelo Hermes.", requiresConfiguration: "requer configuração", activate: "Ativar {{name}}",
      writeOnly: "{{key}} (somente gravação)", configuredReplace: "Configurado · digite para substituir", unconfigured: "Não configurado", clearValue: "Apagar valor", clearConfirm: "Apagar {{key}} de {{name}}?",
      saveCredentials: "Salvar credenciais", test: "Testar canal", empty: "Nenhum canal informado.",
    },
    secrets: {
      title: "Segredos somente para gravação", description: "A interface sabe apenas se um valor está configurado. Ela nunca recebe nem mostra seu conteúdo.", managedValue: "Valor gerenciado pelo Hermes",
      newValue: "Novo valor para {{name}}", save: "Salvar valor", delete: "Excluir", deleteConfirm: "Excluir {{name}}? O Hermes deixará de ter acesso a este valor.", empty: "O Hermes não informou segredos configuráveis.", saved: "{{name}} salvo sem expor seu valor.",
    },
    management: {
      title: "Gerenciar agente", description: "Mova este agente para outro gateway ou exclua-o do Hermes.", protected: "protegido", managed: "gerenciado",
      agent: "Agente", currentGateway: "Gateway atual", transferDisclosure: "Ao mover, são preservados identidade, memória, sessões, skills e automações cron. As credenciais não são copiadas; caminhos e ferramentas locais podem exigir ajustes no gateway de destino.",
      defaultProtected: "O perfil default é essencial para administrar o Hermes e não pode ser movido nem excluído pelo Agent Control.",
      reconcileBlocked: "As operações deste agente estão bloqueadas neste dispositivo até recarregar o Agent Control e verificar seu estado.",
      moveTitle: "Mover para outro gateway", moveDescription: "Transfira o agente e preserve seu conteúdo em um gateway compatível.", noDestination: "Nenhum outro gateway compatível tem importação e rollback/exclusão de perfis verificados.", move: "Mover agente",
      deleteTitle: "Excluir agente", deleteDescription: "Exclui permanentemente o perfil e os dados associados informados pelo Hermes.", delete: "Excluir agente",
      warningsTitle: "A operação terminou com avisos", moveEyebrow: "Transferência de perfil", moveDialogTitle: "Mover {{name}}", moveDialogDescription: "Escolha o gateway de destino e confirme com o nome técnico exato.", destinationGateway: "Gateway de destino",
      typeToConfirm: "Digite {{name}} para confirmar", cancel: "Cancelar", cancelMove: "Cancelar transferência", moving: "Movendo…", confirmMove: "Confirmar transferência",
      deleteEyebrow: "Ação irreversível", deleteDialogTitle: "Excluir {{name}}", deleteDialogDescription: "O Hermes excluirá este agente. Esta ação não pode ser desfeita; confirme com o nome técnico exato.", cancelDelete: "Cancelar exclusão", deleting: "Excluindo…", confirmDelete: "Excluir definitivamente",
      moved: "{{name}} foi movido para o gateway de destino.", deleted: "{{name}} foi excluído.", localCacheWarning: "Não foi possível limpar totalmente o cache local deste dispositivo; limpe-o nas Configurações.", committedRefreshError: "O agente já foi {{action}}, mas não foi possível atualizar esta tela. Recarregue o Agent Control.", outcomeUnknown: "O Hermes não confirmou o resultado. O Agent Control reconciliou o estado disponível e bloqueou o reenvio. Verifique onde está o agente antes de iniciar outra operação.", outcomeUnknownRefresh: "O Hermes não confirmou o resultado e o Agent Control não conseguiu reconciliá-lo. Não repita a operação: recarregue o Agent Control e verifique o agente primeiro.", movedAction: "movido", deletedAction: "excluído",
    },
    route: { aria: "Destino da configuração", gateway: "Gateway", profile: "Perfil Hermes", contract: "Contrato", unverified: "não verificado", exactMethods: "{{count}} métodos exatos" },
    offline: "A administração está bloqueada sem conexão. Os dados visíveis podem estar desatualizados.",
    errors: { read: "Não foi possível ler a configuração do Hermes.", rejected: "O Hermes rejeitou a operação.", heading: "Não foi possível concluir a operação" },
    unavailable: { title: "Sem funções administrativas verificadas", selectedProfile: "selecionado", body: "O perfil {{profile}} não informou métodos exatos de administração. O Agent Control mantém todos os controles ocultos." },
    loading: "Consultando o Hermes…",
  },
} as const;

export const adminResources = { es, en, fr, de, pt } as const;
