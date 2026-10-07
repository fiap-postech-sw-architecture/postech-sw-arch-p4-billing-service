// initContainer replica-set do Job billing-inicializacao (mongosh --nodb
// --file): inicia o replica set de um no e cria os usuarios do servico.
// Idempotente. O root nasce no primeiro boot do volume, pelo entrypoint da
// imagem do MongoDB; as senhas vem do ambiente (Secret billing-mongo), nunca
// de argumento, e nao vao para a saida.
const membro = process.env.MONGO_MEMBRO;
const prazo = Date.now() + 150 * 1000;

function segredo(nome) {
  const valor = process.env[nome];
  if (!valor) {
    throw new Error(`${nome} is empty`);
  }
  return valor;
}

// Repete a condicao a cada 2 s ate ela valer ou o prazo vencer.
function esperar(descricao, condicao) {
  for (;;) {
    let motivo = "not yet";
    try {
      if (condicao()) {
        return;
      }
    } catch (erro) {
      motivo = erro.codeName || erro.name;
    }
    if (Date.now() > prazo) {
      throw new Error(`timed out waiting for ${descricao} (${motivo})`);
    }
    print(`waiting for ${descricao} (${motivo})`);
    sleep(2000);
  }
}

let conexao;
esperar(`mongod at ${membro}`, () => {
  conexao = new Mongo(`mongodb://${membro}/?directConnection=true`);
  return conexao.getDB("admin").runCommand({ ping: 1 }).ok === 1;
});
const admin = conexao.getDB("admin");
admin.auth("root", segredo("MONGO_INITDB_ROOT_PASSWORD"));

// O membro se anuncia pelo nome no Service headless: o mongod confere que o
// nome aponta para ele mesmo.
try {
  admin.runCommand({ replSetGetStatus: 1 });
  print("replica set rs0 already initiated");
} catch (erro) {
  if (erro.codeName !== "NotYetInitialized") {
    throw erro;
  }
  admin.runCommand({
    replSetInitiate: { _id: "rs0", members: [{ _id: 0, host: membro }] },
  });
  print(`replica set rs0 initiated with ${membro}`);
}
esperar("the primary", () => admin.runCommand({ hello: 1 }).isWritablePrimary);

// billing: os processos e o Job (indices e validadores pedem dbAdmin).
// exporter: o mongodb_exporter (replSetGetStatus e dbStats, oplog em local).
const usuarios = [
  {
    banco: conexao.getDB("billing"),
    user: "billing",
    senha: "MONGO_BILLING_PASSWORD",
    roles: [
      { role: "readWrite", db: "billing" },
      { role: "dbAdmin", db: "billing" },
    ],
  },
  {
    banco: admin,
    user: "exporter",
    senha: "MONGO_EXPORTER_PASSWORD",
    roles: [
      { role: "clusterMonitor", db: "admin" },
      { role: "read", db: "local" },
    ],
  },
];
for (const usuario of usuarios) {
  if (usuario.banco.getUser(usuario.user)) {
    print(`user ${usuario.user} already exists`);
    continue;
  }
  usuario.banco.createUser({
    user: usuario.user,
    pwd: segredo(usuario.senha),
    roles: usuario.roles,
  });
  print(`user ${usuario.user} created`);
}
