(ns com.blockether.vis.lang.python.core-test
  "What this pack promises the engine before any of its tools run: the descriptor
   the language interface discovers it by, and the namespaces a native image must
   keep for a registration that stays lazy on the JVM."
  (:require [clojure.edn :as edn]
            [clojure.java.io :as io]
            [com.blockether.vis.lang.interface.packs :as packs]
            [com.blockether.vis.lang.python.core :as core]
            [lazytest.core :refer [defdescribe expect it]]))

(defdescribe pack-descriptor-test
             (it "names this pack's explicit initializer and its Python surface"
                 (expect (some #{{:pack "python"
                                  :register 'com.blockether.vis.lang.python.core/register!
                                  :python ["language_surface_python.py"]}}
                               (packs/descriptors)))
                 (expect (ifn? core/register!)))
             (it "ships the Python surface the descriptor declares"
                 (expect (some? (io/resource "vis-extensions/language_surface_python.py")))))

(defdescribe native-preload-test
             (it "declares the namespaces a native image must retain for this pack"
                 (let [declared (edn/read-string (slurp (io/resource
                                                          "META-INF/vis/native-preload.edn")))]
                   (expect (vector? declared))
                   (expect (some #{'com.blockether.vis.lang.python.core} declared)))))
