const es = {
  adminConfig: {
    tabs: { general: "General", identity: "Identidad", tools: "Herramientas", integrations: "Integraciones", secrets: "Secretos" },
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
    route: { aria: "Destino de configuración", gateway: "Gateway", profile: "Perfil Hermes", contract: "Contrato", unverified: "sin verificar", exactMethods: "{{count}} métodos exactos" },
    offline: "Administración bloqueada sin conexión. Los datos visibles pueden estar desactualizados.",
    errors: { read: "No se pudo leer la configuración de Hermes.", rejected: "Hermes rechazó la operación.", heading: "No se completó la operación" },
    unavailable: { title: "Sin funciones administrativas verificadas", selectedProfile: "seleccionado", body: "El perfil {{profile}} no anunció métodos exactos de administración. Agent Control mantiene ocultos todos los controles." },
    loading: "Consultando Hermes…",
  },
} as const;

const en = {
  adminConfig: {
    tabs: { general: "General", identity: "Identity", tools: "Tools", integrations: "Integrations", secrets: "Secrets" },
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
    route: { aria: "Configuration target", gateway: "Gateway", profile: "Hermes profile", contract: "Contract", unverified: "unverified", exactMethods: "{{count}} exact methods" },
    offline: "Administration is blocked while offline. Visible data may be out of date.",
    errors: { read: "The Hermes configuration could not be read.", rejected: "Hermes rejected the operation.", heading: "The operation could not be completed" },
    unavailable: { title: "No verified administration features", selectedProfile: "selected", body: "The {{profile}} profile did not report exact administration methods. Agent Control keeps all controls hidden." },
    loading: "Querying Hermes…",
  },
} as const;

const fr = {
  adminConfig: {
    tabs: { general: "Général", identity: "Identité", tools: "Outils", integrations: "Intégrations", secrets: "Secrets" },
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
    route: { aria: "Cible de configuration", gateway: "Gateway", profile: "Profil Hermes", contract: "Contrat", unverified: "non vérifié", exactMethods: "{{count}} méthodes exactes" },
    offline: "L’administration est bloquée hors ligne. Les données visibles peuvent être obsolètes.",
    errors: { read: "Impossible de lire la configuration Hermes.", rejected: "Hermes a rejeté l’opération.", heading: "L’opération n’a pas pu être terminée" },
    unavailable: { title: "Aucune fonction d’administration vérifiée", selectedProfile: "sélectionné", body: "Le profil {{profile}} n’a signalé aucune méthode d’administration exacte. Agent Control masque tous les contrôles." },
    loading: "Interrogation de Hermes…",
  },
} as const;

const de = {
  adminConfig: {
    tabs: { general: "Allgemein", identity: "Identität", tools: "Werkzeuge", integrations: "Integrationen", secrets: "Geheimnisse" },
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
    route: { aria: "Konfigurationsziel", gateway: "Gateway", profile: "Hermes-Profil", contract: "Vertrag", unverified: "nicht verifiziert", exactMethods: "{{count}} exakte Methoden" },
    offline: "Die Verwaltung ist offline gesperrt. Sichtbare Daten können veraltet sein.",
    errors: { read: "Die Hermes-Konfiguration konnte nicht gelesen werden.", rejected: "Hermes hat den Vorgang abgelehnt.", heading: "Der Vorgang konnte nicht abgeschlossen werden" },
    unavailable: { title: "Keine verifizierten Verwaltungsfunktionen", selectedProfile: "ausgewählt", body: "Das Profil {{profile}} hat keine exakten Verwaltungsmethoden gemeldet. Agent Control blendet alle Steuerelemente aus." },
    loading: "Hermes wird abgefragt…",
  },
} as const;

const pt = {
  adminConfig: {
    tabs: { general: "Geral", identity: "Identidade", tools: "Ferramentas", integrations: "Integrações", secrets: "Segredos" },
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
    route: { aria: "Destino da configuração", gateway: "Gateway", profile: "Perfil Hermes", contract: "Contrato", unverified: "não verificado", exactMethods: "{{count}} métodos exatos" },
    offline: "A administração está bloqueada sem conexão. Os dados visíveis podem estar desatualizados.",
    errors: { read: "Não foi possível ler a configuração do Hermes.", rejected: "O Hermes rejeitou a operação.", heading: "Não foi possível concluir a operação" },
    unavailable: { title: "Sem funções administrativas verificadas", selectedProfile: "selecionado", body: "O perfil {{profile}} não informou métodos exatos de administração. O Agent Control mantém todos os controles ocultos." },
    loading: "Consultando o Hermes…",
  },
} as const;

export const adminResources = { es, en, fr, de, pt } as const;
